"""Chore audit-log repository: who created / edited / archived / restored a chore."""

from __future__ import annotations

from engine import _cached, _now, get_connection


def _log_chore_audit(conn, family_id, chore_id, chore_name, actor_id, action, detail):
    """Write one chore-audit row using the caller's connection (same txn)."""
    actor_name = ""
    if actor_id:
        a = conn.execute("SELECT name FROM users WHERE id = ?", (actor_id,)).fetchone()
        actor_name = a["name"] if a else ""
    conn.execute(
        """
        INSERT INTO chore_audit (family_id, chore_id, chore_name, actor_id,
                                 actor_name, action, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (family_id, chore_id, chore_name, actor_id, actor_name, action, detail, _now()),
    )


@_cached
def chore_audit_log(family_id: int, limit: int = 50) -> list[dict]:
    """Who changed chores in this family (newest first)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM chore_audit WHERE family_id = ? ORDER BY id DESC LIMIT ?",
            (family_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
