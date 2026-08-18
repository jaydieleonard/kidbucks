"""Penalties repository: penalty templates and applying one (a negative txn)."""

from __future__ import annotations

from config import TXN_PENALTY
from engine import _cached, _now, get_connection


def create_penalty(
    family_id: int, name: str, value: int, description: str = "",
    created_by: int | None = None,
) -> int:
    with get_connection() as conn:
        return conn.insert(
            """
            INSERT INTO penalties (family_id, name, description, value, active,
                                   created_by, created_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (family_id, name, description, value, created_by, _now()),
        )


def update_penalty(penalty_id: int, name: str, value: int, description: str = "") -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE penalties SET name = ?, description = ?, value = ? WHERE id = ?",
            (name, description, value, penalty_id),
        )


def set_penalty_active(penalty_id: int, active: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE penalties SET active = ? WHERE id = ?", (int(active), penalty_id)
        )


@_cached
def list_penalties(family_id: int, active_only: bool = False) -> list[dict]:
    query = "SELECT * FROM penalties WHERE family_id = ?"
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY active DESC, name COLLATE NOCASE"
    with get_connection() as conn:
        rows = conn.execute(query, (family_id,)).fetchall()
    return [dict(r) for r in rows]


def apply_penalty(
    kid_id: int, penalty_id: int, applied_by: int, note: str = ""
) -> bool:
    """Record a penalty against a kid and deduct the bucks (a negative txn)."""
    with get_connection() as conn:
        pen = conn.execute(
            "SELECT name, value FROM penalties WHERE id = ?", (penalty_id,)
        ).fetchone()
        if not pen:
            return False
        app_id = conn.insert(
            """
            INSERT INTO penalty_applications (kid_id, penalty_id, value, note,
                                              applied_by, applied_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (kid_id, penalty_id, pen["value"], note, applied_by, _now()),
        )
        reason = f"Penalty: {pen['name']}"
        if note:
            reason += f" ({note})"
        conn.execute(
            """
            INSERT INTO transactions (kid_id, amount, type, reason, ref_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (kid_id, -abs(pen["value"]), TXN_PENALTY, reason, app_id, _now()),
        )
    return True
