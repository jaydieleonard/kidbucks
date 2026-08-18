"""Core behaviour tests — the regression net for refactoring.

These assert the business rules that must never silently change: the ledger,
family-scoped auth, chore recurrence/sharing, redemption guards, penalties,
pocket-money tracking, notifications and the chore audit log.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import auth
import db
import seed


# --- auth -----------------------------------------------------------------

def test_family_scoped_auth(seeded):
    fam = seeded
    assert auth.authenticate_parent(fam, "mom", "mom123") is not None
    assert auth.authenticate_parent(fam, "mom", "wrong") is None
    assert auth.authenticate_kid(fam, "Ava", "1111") is not None
    assert auth.authenticate_kid(fam, "Ava", "0000") is None


def test_admin_flag(seeded):
    fam = seeded
    assert auth.authenticate_parent(fam, "mom", "mom123")["is_admin"] == 1
    assert auth.authenticate_parent(fam, "dad", "dad123")["is_admin"] == 0


def test_usernames_unique_per_family_not_globally(seeded):
    fam1 = seeded
    fam2, _ = db.create_family("Second Family")
    h, s = auth.hash_secret("pw")
    db.create_user(fam2, "Mum", "parent", h, s, username="mom", is_admin=True)
    # same username 'mom' exists in both families independently
    assert auth.authenticate_parent(fam2, "mom", "pw") is not None
    assert auth.authenticate_parent(fam2, "mom", "mom123") is None  # fam1 pw


def test_login_token_roundtrip_tamper_expiry(monkeypatch):
    monkeypatch.setenv("KIDBUCKS_SECRET", "unit-test-secret")
    tok = auth.make_login_token(7, 3)
    assert auth.read_login_token(tok) == (7, 3)
    assert auth.read_login_token(tok[:-1] + ("0" if tok[-1] != "0" else "1")) is None
    assert auth.read_login_token("nonsense") is None
    monkeypatch.setenv("KIDBUCKS_SECRET", "different-secret")
    assert auth.read_login_token(tok) is None


# --- chores / ledger ------------------------------------------------------

def test_period_keys():
    d = datetime(2026, 8, 12, 9, 0, 0)
    assert db._period_key("daily", d) == "2026-08-12"
    assert db._period_key("weekly", d) == d.strftime("%G-W%V")
    assert db._period_key("monthly", d) == "2026-08"
    assert db._period_key("once", d) == ""


def test_chore_submit_approve_pays_ledger(seeded):
    fam = seeded
    ava = db.get_kid_by_name(fam, "Ava")["id"]
    mom = db.get_parent_by_username(fam, "mom")["id"]
    start = db.get_balance(ava)
    chore = next(c for c in db.available_chores_for_kid(ava) if c["name"] == "Feed the dog")
    sid = db.submit_chore(ava, chore["id"])
    assert sid is not None
    assert db.submit_chore(ava, chore["id"]) is None  # no double-pending
    assert db.approve_chore_submission(sid, mom) is True
    assert db.get_balance(ava) == start + chore["value"]


def test_one_time_chore_archived_on_approval(seeded):
    fam = seeded
    ben = db.get_kid_by_name(fam, "Ben")["id"]
    mom = db.get_parent_by_username(fam, "mom")["id"]
    onetime = next(c for c in db.list_chores(fam, active_only=True)
                   if c["recurrence"] == "once")
    sid = db.submit_chore(ben, onetime["id"])
    db.approve_chore_submission(sid, mom)
    assert db.get_chore(onetime["id"])["active"] == 0


def test_shared_chore_is_first_come(seeded):
    fam = seeded
    ava = db.get_kid_by_name(fam, "Ava")["id"]
    ben = db.get_kid_by_name(fam, "Ben")["id"]
    mom = db.get_parent_by_username(fam, "mom")["id"]
    bins = next(c for c in db.list_chores(fam, active_only=True) if c["shared"])
    sid = db.submit_chore(ava, bins["id"])
    assert sid is not None
    # locked for the family this period
    assert bins["id"] not in [c["id"] for c in db.available_chores_for_kid(ben)]
    assert db.submit_chore(ben, bins["id"]) is None
    # rejecting frees it again
    db.reject_chore_submission(sid, mom)
    assert bins["id"] in [c["id"] for c in db.available_chores_for_kid(ben)]


def test_daily_chore_yesterday_does_not_block_today(seeded):
    fam = seeded
    ava = db.get_kid_by_name(fam, "Ava")["id"]
    feed = next(c for c in db.list_chores(fam, active_only=True)
                if c["name"] == "Feed the dog")
    ykey = db._period_key("daily", datetime.now() - timedelta(days=1))
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO chore_submissions (kid_id, chore_id, status, value, "
            "period_key, submitted_at) VALUES (?, ?, 'approved', ?, ?, ?)",
            (ava, feed["id"], feed["value"], ykey, db._now()),
        )
    assert feed["id"] in [c["id"] for c in db.available_chores_for_kid(ava)]


# --- redemptions ----------------------------------------------------------

def test_redemption_cannot_go_negative(seeded):
    fam = seeded
    mom = db.get_parent_by_username(fam, "mom")["id"]
    h, s = auth.hash_secret("0000")
    kid = db.create_user(fam, "MaxKid", "kid", h, s)
    db.link_parent_kid(mom, kid)
    db.add_transaction(kid, 100, db.TXN_CHORE, "seed")
    pm = db.pocket_money_option(fam)
    assert db.effective_rate(pm["id"], kid) == 10.0
    assert math.floor(db.get_balance(kid) / 10.0) == 10  # max R10
    assert db.request_redemption(kid, pm["id"], 11) is None      # over balance
    rid = db.request_redemption(kid, pm["id"], 10)               # exactly balance
    assert rid is not None
    ok, _ = db.approve_redemption(rid, mom)
    assert ok and db.get_balance(kid) == 0
    assert db.request_redemption(kid, pm["id"], 1) is None       # nothing at zero


def test_per_child_rate_pocket_money_only(seeded):
    fam = seeded
    ava = db.get_kid_by_name(fam, "Ava")["id"]
    ben = db.get_kid_by_name(fam, "Ben")["id"]
    pm = db.pocket_money_option(fam)
    stime = next(o for o in db.list_redemption_options(fam, active_only=True)
                 if o["name"] == "Screen Time")
    assert db.effective_rate(pm["id"], ava) == 10.0     # default
    assert db.effective_rate(pm["id"], ben) == 8.0      # seeded override
    db.set_kid_rate(stime["id"], ben, 1.0)
    assert db.effective_rate(stime["id"], ben) == 2.0   # not per_child -> ignored


# --- penalties ------------------------------------------------------------

def test_penalty_can_push_negative_but_blocks_redeem(seeded):
    fam = seeded
    ava = db.get_kid_by_name(fam, "Ava")["id"]
    mom = db.get_parent_by_username(fam, "mom")["id"]
    # drain balance to a small amount then penalise below zero
    bal = db.get_balance(ava)
    pen = db.list_penalties(fam, active_only=True)[0]
    for _ in range(int(bal / pen["value"]) + 1):
        db.apply_penalty(ava, pen["id"], mom, "test")
    assert db.get_balance(ava) < 0
    pm = db.pocket_money_option(fam)
    assert db.request_redemption(ava, pm["id"], 1) is None


# --- pocket money ---------------------------------------------------------

def test_pocket_money_monthly_and_paid(seeded):
    fam = seeded
    ava = db.get_kid_by_name(fam, "Ava")["id"]
    month = db.current_month()
    hist = db.pocket_money_monthly_history(ava)
    assert len(hist) >= 2  # seed adds this month + last month
    this = db.pocket_money_month_total(ava, month)
    assert this["unpaid_count"] == 1
    r = db.pocket_money_redemptions(ava, month)[0]
    db.mark_redemption_paid(r["id"], True)
    assert db.pocket_money_month_total(ava, month)["unpaid_count"] == 0


def test_currency_detection_and_option():
    assert db.is_currency_unit("R") and not db.is_currency_unit("min")


def test_earned_this_month(seeded):
    fam = seeded
    ava = db.get_kid_by_name(fam, "Ava")["id"]
    ben = db.get_kid_by_name(fam, "Ben")["id"]
    month = db.current_month()
    assert db.earned_this_month(ava, month) == 200   # seeded starting chore
    assert db.earned_this_month(ben, month) == 0


# --- notifications --------------------------------------------------------

def test_kid_reviewed_items_and_last_seen(seeded):
    fam = seeded
    ava = db.get_kid_by_name(fam, "Ava")["id"]
    reviewed = db.kid_reviewed_items(ava)
    assert len(reviewed) >= 1                                   # seeded approvals
    # `since` filter is deterministic against fixed bounds
    assert db.kid_reviewed_items(ava, since="2999-01-01T00:00:00") == []
    assert len(db.kid_reviewed_items(ava, since="2000-01-01T00:00:00")) == len(reviewed)
    db.touch_last_seen(ava)
    assert db.get_user(ava)["last_seen_at"] is not None


# --- audit ----------------------------------------------------------------

def test_chore_audit_records_actor_and_diff(seeded):
    fam = seeded
    mom = db.get_parent_by_username(fam, "mom")["id"]
    dad = db.get_parent_by_username(fam, "dad")["id"]
    cid = db.create_chore(fam, "Vacuum", 12, "weekly", False, created_by=mom)
    assert db.chore_audit_log(fam)[0]["action"] == "created"
    db.update_chore(cid, "Vacuum", 20, "daily", False, actor_id=dad)
    top = db.chore_audit_log(fam)[0]
    assert top["action"] == "edited" and top["actor_name"] == "Dad"
    assert "12→20" in top["detail"] and "Weekly→Daily" in top["detail"]
    db.set_chore_active(cid, False, actor_id=mom)
    assert db.chore_audit_log(fam)[0]["action"] == "archived"


# --- new-family defaults --------------------------------------------------

def test_new_family_gets_starter_content(clean):
    fam_id, mom_id = clean
    db.seed_family_defaults(fam_id, created_by=mom_id)
    assert len(db.list_chores(fam_id, active_only=True)) == 4
    assert len(db.list_penalties(fam_id, active_only=True)) == 3
    assert len(db.list_redemption_options(fam_id, active_only=True)) == 2
    assert db.pocket_money_option(fam_id)["name"] == "Pocket Money"
