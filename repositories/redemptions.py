"""Redemptions repository: converting KidBucks into rewards.

Owns three closely related things:

* **Redemption options** — a family's reward menu (name, unit, bucks-per-unit
  rate) plus optional per-child rate overrides.
* **Redemption requests** — a kid asks to convert bucks; a parent approves,
  which writes the negative ledger transaction that spends the bucks.
* **Pocket money** — the subset of approved redemptions whose unit is real
  money (a currency symbol), tracked per calendar month with a `paid` flag.
"""

from __future__ import annotations

import math
from datetime import datetime

from config import TXN_REDEMPTION
from engine import _cached, _now, get_connection
from formatting import fmt_units, is_currency_unit


# --- Redemption options (rates) --------------------------------------------

def create_redemption_option(
    family_id: int, name: str, unit: str, bucks_per_unit: float,
    per_child: bool = False,
) -> int:
    with get_connection() as conn:
        return conn.insert(
            """
            INSERT INTO redemption_options (family_id, name, unit, bucks_per_unit,
                                            per_child, active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (family_id, name, unit, bucks_per_unit, int(per_child), _now()),
        )


def update_redemption_rate(option_id: int, bucks_per_unit: float) -> None:
    """Change a conversion rate. Callers must ensure the user is an admin."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE redemption_options SET bucks_per_unit = ? WHERE id = ?",
            (bucks_per_unit, option_id),
        )


def update_redemption_option(
    option_id: int, name: str, unit: str, bucks_per_unit: float,
    per_child: bool | None = None,
) -> None:
    with get_connection() as conn:
        if per_child is None:
            conn.execute(
                "UPDATE redemption_options SET name = ?, unit = ?, bucks_per_unit = ? "
                "WHERE id = ?",
                (name, unit, bucks_per_unit, option_id),
            )
        else:
            conn.execute(
                "UPDATE redemption_options SET name = ?, unit = ?, bucks_per_unit = ?, "
                "per_child = ? WHERE id = ?",
                (name, unit, bucks_per_unit, int(per_child), option_id),
            )


def set_redemption_option_active(option_id: int, active: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE redemption_options SET active = ? WHERE id = ?",
            (int(active), option_id),
        )


def get_redemption_option(option_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM redemption_options WHERE id = ?", (option_id,)
        ).fetchone()
    return dict(row) if row else None


@_cached
def list_redemption_options(family_id: int, active_only: bool = False) -> list[dict]:
    query = "SELECT * FROM redemption_options WHERE family_id = ?"
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY active DESC, name COLLATE NOCASE"
    with get_connection() as conn:
        rows = conn.execute(query, (family_id,)).fetchall()
    return [dict(r) for r in rows]


# --- Per-child rate overrides ----------------------------------------------

@_cached
def effective_rate(option_id: int, kid_id: int) -> float | None:
    """The bucks-per-unit rate for this kid: their override, else the default.

    Per-child overrides only apply when the option has `per_child` enabled;
    otherwise the family default is always used. Returns None if the option
    doesn't exist.
    """
    with get_connection() as conn:
        opt = conn.execute(
            "SELECT bucks_per_unit, per_child FROM redemption_options WHERE id = ?",
            (option_id,),
        ).fetchone()
        if not opt:
            return None
        if opt["per_child"]:
            override = conn.execute(
                "SELECT bucks_per_unit FROM redemption_rates "
                "WHERE option_id = ? AND kid_id = ?",
                (option_id, kid_id),
            ).fetchone()
            if override:
                return override[0]
    return opt["bucks_per_unit"]


def get_kid_rate_overrides(option_id: int) -> dict[int, float]:
    """{kid_id: rate} for kids with an explicit override on this option."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT kid_id, bucks_per_unit FROM redemption_rates WHERE option_id = ?",
            (option_id,),
        ).fetchall()
    return {r["kid_id"]: r["bucks_per_unit"] for r in rows}


def set_kid_rate(option_id: int, kid_id: int, bucks_per_unit: float) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO redemption_rates (option_id, kid_id, bucks_per_unit)
            VALUES (?, ?, ?)
            ON CONFLICT(option_id, kid_id)
            DO UPDATE SET bucks_per_unit = excluded.bucks_per_unit
            """,
            (option_id, kid_id, bucks_per_unit),
        )


def clear_kid_rate(option_id: int, kid_id: int) -> None:
    """Remove a kid's override so they fall back to the option default."""
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM redemption_rates WHERE option_id = ? AND kid_id = ?",
            (option_id, kid_id),
        )


# --- Redemption requests ---------------------------------------------------

def _redemption_cost(bucks_per_unit: float, units: float) -> int:
    """Bucks needed for `units`, rounded up so kids never underpay."""
    return int(math.ceil(bucks_per_unit * units))


def redemption_cost(bucks_per_unit: float, units: float) -> int:
    """Public helper so pages can preview a cost before submitting."""
    return _redemption_cost(bucks_per_unit, units)


def request_redemption(kid_id: int, option_id: int, units: float) -> int | None:
    """Kid asks to convert bucks. Cost is snapshotted; awaits parent approval.

    A kid can never redeem into the negative: this is blocked when the cost
    exceeds the current balance (which also blocks any redemption while the
    balance is already zero or negative). Only parent penalties/demerits may
    push a balance below zero. Returns request id, or None if the option is
    missing/inactive, units are non-positive, or the kid can't afford it.
    """
    if units <= 0:
        return None
    with get_connection() as conn:
        opt = conn.execute(
            "SELECT * FROM redemption_options WHERE id = ? AND active = 1",
            (option_id,),
        ).fetchone()
        if not opt:
            return None
        # Honour a per-child rate override when the option allows it.
        rate = opt["bucks_per_unit"]
        if opt["per_child"]:
            ov = conn.execute(
                "SELECT bucks_per_unit FROM redemption_rates "
                "WHERE option_id = ? AND kid_id = ?",
                (option_id, kid_id),
            ).fetchone()
            if ov:
                rate = ov[0]
        cost = _redemption_cost(rate, units)
        balance = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE kid_id = ?",
            (kid_id,),
        ).fetchone()[0]
        if cost > balance:
            return None
        return conn.insert(
            """
            INSERT INTO redemption_requests (kid_id, option_id, option_name, units,
                                             unit_label, bucks_spent, status,
                                             requested_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (kid_id, option_id, opt["name"], units, opt["unit"], cost, _now()),
        )


@_cached
def pending_redemptions_for_parent(parent_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.kid_id, r.option_name, r.units, r.unit_label,
                   r.bucks_spent, r.requested_at,
                   u.name AS kid_name, u.emoji AS kid_emoji,
                   COALESCE((SELECT SUM(amount) FROM transactions
                              WHERE kid_id = r.kid_id), 0) AS kid_balance
              FROM redemption_requests r
              JOIN users u ON u.id = r.kid_id
              JOIN parent_kid pk ON pk.kid_id = r.kid_id
             WHERE pk.parent_id = ? AND r.status = 'pending'
             ORDER BY r.requested_at ASC
            """,
            (parent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@_cached
def kid_redemptions(kid_id: int, limit: int = 25) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, option_name, units, unit_label, bucks_spent, status,
                   requested_at, reviewed_at
              FROM redemption_requests
             WHERE kid_id = ?
             ORDER BY id DESC
             LIMIT ?
            """,
            (kid_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def approve_redemption(request_id: int, reviewer_id: int) -> tuple[bool, str]:
    """Approve a redemption, deducting the bucks.

    Re-checks the balance at approval time (it may have dropped since the
    request, e.g. after a penalty) and refuses if it would go negative — a
    redemption must never push a kid below zero. Returns (ok, message).
    """
    with get_connection() as conn:
        req = conn.execute(
            "SELECT * FROM redemption_requests WHERE id = ? AND status = 'pending'",
            (request_id,),
        ).fetchone()
        if not req:
            return False, "Request no longer pending."
        balance = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE kid_id = ?",
            (req["kid_id"],),
        ).fetchone()[0]
        if req["bucks_spent"] > balance:
            return False, "Kid no longer has enough bucks for this redemption."
        conn.execute(
            "UPDATE redemption_requests SET status = 'approved', reviewed_by = ?, "
            "reviewed_at = ? WHERE id = ?",
            (reviewer_id, _now(), request_id),
        )
        reason = (
            f"Redeemed: {fmt_units(req['units'], req['unit_label'])} "
            f"of {req['option_name']}"
        )
        conn.execute(
            """
            INSERT INTO transactions (kid_id, amount, type, reason, ref_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (req["kid_id"], -abs(req["bucks_spent"]), TXN_REDEMPTION, reason,
             request_id, _now()),
        )
    return True, "Redemption approved."


def reject_redemption(request_id: int, reviewer_id: int) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE redemption_requests SET status = 'rejected', reviewed_by = ?, "
            "reviewed_at = ? WHERE id = ? AND status = 'pending'",
            (reviewer_id, _now(), request_id),
        )
        return cur.rowcount > 0


# --- Pocket money tracking -------------------------------------------------
# "Pocket money" = an approved redemption whose unit is real money (a currency
# symbol like R), as opposed to screen time. Tracked by the month it was
# approved, with a `paid` flag the parent sets once the cash is handed over.

def current_month() -> str:
    return datetime.now().strftime("%Y-%m")


def month_label(month: str) -> str:
    """'2026-07' -> 'July 2026' (falls back to the raw value)."""
    try:
        return datetime.strptime(month + "-01", "%Y-%m-%d").strftime("%B %Y")
    except (ValueError, TypeError):
        return month


def _redemption_month(row: dict) -> str:
    ts = row.get("reviewed_at") or row.get("requested_at") or ""
    return ts[:7]


def mark_redemption_paid(request_id: int, paid: bool = True) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE redemption_requests SET paid = ?, paid_at = ? WHERE id = ?",
            (int(paid), _now() if paid else None, request_id),
        )


@_cached
def pocket_money_redemptions(kid_id: int, month: str | None = None) -> list[dict]:
    """Approved real-money redemptions for a kid, newest first. `month` filters
    to a 'YYYY-MM' by approval date."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, option_name, units, unit_label, bucks_spent, requested_at,
                   reviewed_at, paid, paid_at
              FROM redemption_requests
             WHERE kid_id = ? AND status = 'approved'
             ORDER BY COALESCE(reviewed_at, requested_at) DESC
            """,
            (kid_id,),
        ).fetchall()
    result = [dict(r) for r in rows if is_currency_unit(r["unit_label"])]
    if month:
        result = [r for r in result if _redemption_month(r) == month]
    return result


def pocket_money_month_total(kid_id: int, month: str) -> dict:
    """Summary for one month: total/unpaid units and counts."""
    rows = pocket_money_redemptions(kid_id, month)
    return {
        "units": sum(r["units"] for r in rows),
        "unit_label": rows[0]["unit_label"] if rows else "R",
        "count": len(rows),
        "unpaid_units": sum(r["units"] for r in rows if not r["paid"]),
        "unpaid_count": sum(1 for r in rows if not r["paid"]),
    }


@_cached
def pocket_money_monthly_history(kid_id: int) -> list[dict]:
    """Totals grouped by month (newest first): month, units, count, paid_count."""
    agg: dict[str, dict] = {}
    for r in pocket_money_redemptions(kid_id):
        m = _redemption_month(r)
        a = agg.setdefault(
            m,
            {"month": m, "units": 0.0, "count": 0, "paid_count": 0,
             "unit_label": r["unit_label"]},
        )
        a["units"] += r["units"]
        a["count"] += 1
        a["paid_count"] += 1 if r["paid"] else 0
    return sorted(agg.values(), key=lambda x: x["month"], reverse=True)


def pocket_money_option(family_id: int) -> dict | None:
    """The active redemption option used for pocket money (real money).

    Prefers one literally named "Pocket Money", else the first currency-unit
    option. Used to value KidBucks in Rand for the dashboard projections.
    """
    opts = list_redemption_options(family_id, active_only=True)
    named = [o for o in opts if o["name"].strip().lower() == "pocket money"]
    if named:
        return named[0]
    for o in opts:
        if is_currency_unit(o["unit"]):
            return o
    return None
