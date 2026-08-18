"""Ledger repository: the transaction ledger and balances derived from it.

The single source of truth for every wallet balance — a balance is always the
SUM of a kid's transaction rows, never stored directly.
"""

from __future__ import annotations

from engine import _cached, _now, get_connection


def add_transaction(
    kid_id: int, amount: int, txn_type: str, reason: str = "", ref_id: int | None = None
) -> int:
    """Write one ledger row. amount > 0 = bucks in, amount < 0 = bucks out."""
    with get_connection() as conn:
        return conn.insert(
            """
            INSERT INTO transactions (kid_id, amount, type, reason, ref_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (kid_id, amount, txn_type, reason, ref_id, _now()),
        )


@_cached
def get_balance(kid_id: int) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE kid_id = ?",
            (kid_id,),
        ).fetchone()
    return row[0]


@_cached
def kid_summary(kid_id: int) -> dict:
    """Balance plus lifetime earned/spent and count of pending items."""
    with get_connection() as conn:
        balance = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE kid_id = ?",
            (kid_id,),
        ).fetchone()[0]
        earned = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions "
            "WHERE kid_id = ? AND amount > 0",
            (kid_id,),
        ).fetchone()[0]
        spent = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions "
            "WHERE kid_id = ? AND amount < 0",
            (kid_id,),
        ).fetchone()[0]
        pending_chores = conn.execute(
            "SELECT COUNT(*) FROM chore_submissions "
            "WHERE kid_id = ? AND status = 'pending'",
            (kid_id,),
        ).fetchone()[0]
        pending_redemptions = conn.execute(
            "SELECT COUNT(*) FROM redemption_requests "
            "WHERE kid_id = ? AND status = 'pending'",
            (kid_id,),
        ).fetchone()[0]
    return {
        "balance": balance,
        "earned": earned,
        "spent": abs(spent),
        "pending": pending_chores + pending_redemptions,
    }


@_cached
def recent_transactions(kid_id: int, limit: int = 25) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, amount, type, reason
              FROM transactions
             WHERE kid_id = ?
             ORDER BY id DESC
             LIMIT ?
            """,
            (kid_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


@_cached
def earned_this_month(kid_id: int, month: str) -> int:
    """KidBucks earned (positive transactions) by a kid in a given 'YYYY-MM'."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions "
            "WHERE kid_id = ? AND amount > 0 AND substr(created_at, 1, 7) = ?",
            (kid_id, month),
        ).fetchone()
    return row[0]
