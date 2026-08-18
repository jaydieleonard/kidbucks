"""Chores repository: the chore catalogue and the submission → approval flow.

A chore is one-time or recurring (daily/weekly/monthly) and belongs to a family.
Each submission stamps a `period_key` from the chore's recurrence at submit time,
so "already done this period" is a simple equality check; a `shared` chore can be
claimed by only ONE kid in the family per period (first come). Approving a
submission pays the kid via a ledger transaction and retires one-time chores.
Chore edits/archival are recorded through the audit repository.
"""

from __future__ import annotations

from datetime import datetime

from config import BUCK, RECURRENCE_LABELS, TXN_CHORE
from engine import _cached, _now, get_connection
from formatting import fmt_units
from models import Chore
from repositories.audit import _log_chore_audit
from repositories.users import get_user


def _period_key(recurrence: str, dt: datetime) -> str:
    """Which recurrence window `dt` falls in. '' means the chore is one-time."""
    if recurrence == "daily":
        return dt.strftime("%Y-%m-%d")
    if recurrence == "weekly":
        return dt.strftime("%G-W%V")   # ISO year + ISO week (Mon-Sun)
    if recurrence == "monthly":
        return dt.strftime("%Y-%m")
    return ""


def create_chore(
    family_id: int,
    name: str,
    value: int,
    recurrence: str = "once",
    shared: bool = False,
    description: str = "",
    created_by: int | None = None,
) -> int:
    with get_connection() as conn:
        cid = conn.insert(
            """
            INSERT INTO chores (family_id, name, description, value, recurrence,
                                shared, active, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (family_id, name, description, value, recurrence, int(shared),
             created_by, _now()),
        )
        _log_chore_audit(conn, family_id, cid, name, created_by, "created",
                         f"{value} {BUCK}, {RECURRENCE_LABELS.get(recurrence, recurrence)}"
                         + (", family task" if shared else ""))
        return cid


def update_chore(
    chore_id: int,
    name: str,
    value: int,
    recurrence: str,
    shared: bool,
    description: str = "",
    actor_id: int | None = None,
) -> None:
    with get_connection() as conn:
        old = conn.execute("SELECT * FROM chores WHERE id = ?", (chore_id,)).fetchone()
        conn.execute(
            """
            UPDATE chores
               SET name = ?, description = ?, value = ?, recurrence = ?, shared = ?
             WHERE id = ?
            """,
            (name, description, value, recurrence, int(shared), chore_id),
        )
        if old:
            changes = []
            if old["name"] != name:
                changes.append(f"name “{old['name']}”→“{name}”")
            if old["value"] != value:
                changes.append(f"value {old['value']}→{value} {BUCK}")
            if old["recurrence"] != recurrence:
                changes.append(
                    f"recurrence {RECURRENCE_LABELS.get(old['recurrence'], old['recurrence'])}"
                    f"→{RECURRENCE_LABELS.get(recurrence, recurrence)}"
                )
            if bool(old["shared"]) != bool(shared):
                changes.append("family task on" if shared else "family task off")
            if (old["description"] or "") != (description or ""):
                changes.append("description")
            detail = ", ".join(changes) if changes else "no changes"
            _log_chore_audit(conn, old["family_id"], chore_id, name, actor_id,
                             "edited", detail)


def set_chore_active(chore_id: int, active: bool, actor_id: int | None = None) -> None:
    with get_connection() as conn:
        ch = conn.execute("SELECT * FROM chores WHERE id = ?", (chore_id,)).fetchone()
        conn.execute(
            "UPDATE chores SET active = ? WHERE id = ?", (int(active), chore_id)
        )
        if ch:
            _log_chore_audit(conn, ch["family_id"], chore_id, ch["name"], actor_id,
                             "restored" if active else "archived", "")


def get_chore(chore_id: int) -> Chore | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM chores WHERE id = ?", (chore_id,)).fetchone()
    return Chore.from_row(row)


@_cached
def list_chores(family_id: int, active_only: bool = False) -> list[Chore]:
    query = "SELECT * FROM chores WHERE family_id = ?"
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY active DESC, name COLLATE NOCASE"
    with get_connection() as conn:
        rows = conn.execute(query, (family_id,)).fetchall()
    return [Chore.from_row(r) for r in rows]


def _chore_claimed(chore: dict, kid_id: int, family_id: int) -> bool:
    """True if `chore` is currently unavailable to this kid (already claimed).

    One-time: blocked only while the kid has a pending submission (approval
    archives the chore anyway). Recurring: blocked if there's a pending/approved
    submission for the current period — by this kid, or by ANY family kid when the
    chore is `shared`. Rejected submissions never block.
    """
    with get_connection() as conn:
        if chore["recurrence"] == "once":
            row = conn.execute(
                "SELECT 1 FROM chore_submissions "
                "WHERE chore_id = ? AND kid_id = ? AND status = 'pending'",
                (chore["id"], kid_id),
            ).fetchone()
            return row is not None

        pkey = _period_key(chore["recurrence"], datetime.now())
        if chore["shared"]:
            row = conn.execute(
                """
                SELECT 1
                  FROM chore_submissions s
                  JOIN users u ON u.id = s.kid_id
                 WHERE s.chore_id = ? AND u.family_id = ? AND s.period_key = ?
                   AND s.status IN ('pending', 'approved')
                """,
                (chore["id"], family_id, pkey),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT 1 FROM chore_submissions
                 WHERE chore_id = ? AND kid_id = ? AND period_key = ?
                   AND status IN ('pending', 'approved')
                """,
                (chore["id"], kid_id, pkey),
            ).fetchone()
        return row is not None


@_cached
def available_chores_for_kid(kid_id: int) -> list[dict]:
    """Active chores the kid can submit right now (period- and share-aware)."""
    kid = get_user(kid_id)
    if not kid:
        return []
    chores = list_chores(kid["family_id"], active_only=True)
    return [c for c in chores if not _chore_claimed(c, kid_id, kid["family_id"])]


# --- Chore submissions -----------------------------------------------------

def submit_chore(kid_id: int, chore_id: int, note: str = "") -> int | None:
    """Kid claims a chore. Returns submission id, or None if not claimable now."""
    kid = get_user(kid_id)
    chore = get_chore(chore_id)
    if not kid or not chore or not chore["active"]:
        return None
    if chore["family_id"] != kid["family_id"]:
        return None
    if _chore_claimed(chore, kid_id, kid["family_id"]):
        return None
    pkey = _period_key(chore["recurrence"], datetime.now())
    with get_connection() as conn:
        return conn.insert(
            """
            INSERT INTO chore_submissions (kid_id, chore_id, status, value,
                                           period_key, note, submitted_at)
            VALUES (?, ?, 'pending', ?, ?, ?, ?)
            """,
            (kid_id, chore_id, chore["value"], pkey, note, _now()),
        )


@_cached
def pending_chore_submissions_for_parent(parent_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.kid_id, s.value, s.note, s.submitted_at,
                   u.name AS kid_name, u.emoji AS kid_emoji,
                   c.name AS chore_name, c.recurrence, c.shared
              FROM chore_submissions s
              JOIN users u  ON u.id = s.kid_id
              JOIN chores c ON c.id = s.chore_id
              JOIN parent_kid pk ON pk.kid_id = s.kid_id
             WHERE pk.parent_id = ? AND s.status = 'pending'
             ORDER BY s.submitted_at ASC
            """,
            (parent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@_cached
def kid_submissions(kid_id: int, limit: int = 25) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.status, s.value, s.submitted_at, s.reviewed_at,
                   c.name AS chore_name, c.recurrence
              FROM chore_submissions s
              JOIN chores c ON c.id = s.chore_id
             WHERE s.kid_id = ?
             ORDER BY s.id DESC
             LIMIT ?
            """,
            (kid_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


@_cached
def kid_reviewed_items(kid_id: int, since: str | None = None,
                       limit: int = 20) -> list[dict]:
    """A kid's decided chores + redemptions (approved/declined), newest first.

    If `since` is given, only items reviewed after that timestamp are returned
    (used for the "new updates" badge). Each item: kind, title, status, amount,
    detail, when.
    """
    where_c = ("s.kid_id = ? AND s.status IN ('approved', 'rejected') "
               "AND s.reviewed_at IS NOT NULL")
    where_r = ("r.kid_id = ? AND r.status IN ('approved', 'rejected') "
               "AND r.reviewed_at IS NOT NULL")
    params_c: list = [kid_id]
    params_r: list = [kid_id]
    if since:
        where_c += " AND s.reviewed_at > ?"
        params_c.append(since)
        where_r += " AND r.reviewed_at > ?"
        params_r.append(since)
    with get_connection() as conn:
        crows = conn.execute(
            f"SELECT c.name AS title, s.status, s.value AS amount, s.reviewed_at "
            f"FROM chore_submissions s JOIN chores c ON c.id = s.chore_id "
            f"WHERE {where_c}",
            params_c,
        ).fetchall()
        rrows = conn.execute(
            f"SELECT r.option_name AS title, r.status, r.bucks_spent AS amount, "
            f"r.units, r.unit_label, r.reviewed_at "
            f"FROM redemption_requests r WHERE {where_r}",
            params_r,
        ).fetchall()
    items = [
        {"kind": "chore", "title": r["title"], "status": r["status"],
         "amount": r["amount"], "detail": None, "when": r["reviewed_at"]}
        for r in crows
    ] + [
        {"kind": "redemption", "title": r["title"], "status": r["status"],
         "amount": r["amount"], "detail": fmt_units(r["units"], r["unit_label"]),
         "when": r["reviewed_at"]}
        for r in rrows
    ]
    items.sort(key=lambda x: x["when"] or "", reverse=True)
    return items[:limit]


def approve_chore_submission(submission_id: int, reviewer_id: int) -> bool:
    """Approve a pending submission: pay the kid and archive one-time chores."""
    with get_connection() as conn:
        sub = conn.execute(
            "SELECT * FROM chore_submissions WHERE id = ? AND status = 'pending'",
            (submission_id,),
        ).fetchone()
        if not sub:
            return False
        conn.execute(
            "UPDATE chore_submissions SET status = 'approved', reviewed_by = ?, "
            "reviewed_at = ? WHERE id = ?",
            (reviewer_id, _now(), submission_id),
        )
        chore = conn.execute(
            "SELECT name, recurrence FROM chores WHERE id = ?", (sub["chore_id"],)
        ).fetchone()
        reason = f"Chore: {chore['name']}" if chore else "Chore"
        conn.execute(
            """
            INSERT INTO transactions (kid_id, amount, type, reason, ref_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (sub["kid_id"], sub["value"], TXN_CHORE, reason, submission_id, _now()),
        )
        # A one-time chore is retired once it has been approved.
        if chore and chore["recurrence"] == "once":
            conn.execute(
                "UPDATE chores SET active = 0 WHERE id = ?", (sub["chore_id"],)
            )
    return True


def reject_chore_submission(submission_id: int, reviewer_id: int, note: str = "") -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE chore_submissions
               SET status = 'rejected', reviewed_by = ?, reviewed_at = ?,
                   note = CASE WHEN ? = '' THEN note ELSE ? END
             WHERE id = ? AND status = 'pending'
            """,
            (reviewer_id, _now(), note, note, submission_id),
        )
        return cur.rowcount > 0
