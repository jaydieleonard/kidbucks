"""Users repository: parents and kids, plus the parent↔kid links.

Owns the `users` table (both roles) and the `parent_kid` link table that says
which parents can see and approve for which kids. Kid balances returned by the
listing helpers are derived from the ledger via a LEFT JOIN, never stored.
"""

from __future__ import annotations

from engine import _cached, _now, get_connection
from models import User


# --- Users -----------------------------------------------------------------

def create_user(
    family_id: int,
    name: str,
    role: str,
    secret_hash: str,
    salt: str,
    *,
    username: str | None = None,
    is_admin: bool = False,
    emoji: str = "🙂",
) -> int:
    with get_connection() as conn:
        return conn.insert(
            """
            INSERT INTO users (family_id, name, role, is_admin, username,
                               secret_hash, salt, emoji, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (family_id, name, role, int(is_admin), username, secret_hash, salt,
             emoji, _now()),
        )


@_cached
def get_user(user_id: int) -> User | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return User.from_row(row)


def get_parent_by_username(family_id: int, username: str) -> User | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE role = 'parent' AND family_id = ? "
            "AND username = ? COLLATE NOCASE",
            (family_id, username),
        ).fetchone()
    return User.from_row(row)


def get_kid_by_name(family_id: int, name: str) -> User | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE role = 'kid' AND family_id = ? "
            "AND name = ? COLLATE NOCASE",
            (family_id, name),
        ).fetchone()
    return User.from_row(row)


def username_exists(family_id: int, username: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE family_id = ? AND username = ? COLLATE NOCASE",
            (family_id, username),
        ).fetchone()
    return row is not None


def kid_name_exists(family_id: int, name: str) -> bool:
    return get_kid_by_name(family_id, name) is not None


def count_parents(family_id: int) -> int:
    with get_connection() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM users WHERE role = 'parent' AND family_id = ?",
            (family_id,),
        ).fetchone()[0]


@_cached
def list_parents(family_id: int) -> list[User]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE role = 'parent' AND family_id = ? "
            "ORDER BY name COLLATE NOCASE",
            (family_id,),
        ).fetchall()
    return [User.from_row(r) for r in rows]


def update_user_profile(user_id: int, name: str, emoji: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET name = ?, emoji = ? WHERE id = ?",
            (name, emoji, user_id),
        )


def set_admin(user_id: int, is_admin: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id)
        )


def reset_secret(user_id: int, secret_hash: str, salt: str) -> None:
    """Replace a user's PIN/password hash (caller does the hashing)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET secret_hash = ?, salt = ? WHERE id = ?",
            (secret_hash, salt, user_id),
        )


def touch_last_seen(user_id: int) -> None:
    """Mark 'now' as the moment this user last caught up on their updates."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET last_seen_at = ? WHERE id = ?", (_now(), user_id)
        )


def delete_user(user_id: int) -> None:
    """Deletes the user and (via ON DELETE CASCADE) their links & transactions."""
    with get_connection() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# --- Parent <-> kid links --------------------------------------------------

def link_parent_kid(parent_id: int, kid_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO parent_kid (parent_id, kid_id) VALUES (?, ?)",
            (parent_id, kid_id),
        )


def unlink_parent_kid(parent_id: int, kid_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM parent_kid WHERE parent_id = ? AND kid_id = ?",
            (parent_id, kid_id),
        )


def set_kid_parents(kid_id: int, parent_ids: list[int]) -> None:
    """Replace a kid's parent links with exactly `parent_ids`."""
    with get_connection() as conn:
        conn.execute("DELETE FROM parent_kid WHERE kid_id = ?", (kid_id,))
        conn.executemany(
            "INSERT OR IGNORE INTO parent_kid (parent_id, kid_id) VALUES (?, ?)",
            [(pid, kid_id) for pid in parent_ids],
        )


@_cached
def get_kid_parent_ids(kid_id: int) -> list[int]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT parent_id FROM parent_kid WHERE kid_id = ?", (kid_id,)
        ).fetchall()
    return [r[0] for r in rows]


@_cached
def list_kids_for_parent(parent_id: int) -> list[dict]:
    """Kids linked to this parent, each with their current balance."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.name, u.emoji,
                   COALESCE(SUM(t.amount), 0) AS balance
              FROM users u
              JOIN parent_kid pk ON pk.kid_id = u.id
              LEFT JOIN transactions t ON t.kid_id = u.id
             WHERE pk.parent_id = ?
             GROUP BY u.id
             ORDER BY u.name COLLATE NOCASE
            """,
            (parent_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@_cached
def list_all_kids(family_id: int) -> list[dict]:
    """Every kid in a family with their balance."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.name, u.emoji,
                   COALESCE(SUM(t.amount), 0) AS balance
              FROM users u
              LEFT JOIN transactions t ON t.kid_id = u.id
             WHERE u.role = 'kid' AND u.family_id = ?
             GROUP BY u.id
             ORDER BY u.name COLLATE NOCASE
            """,
            (family_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def parent_can_see_kid(parent_id: int, kid_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM parent_kid WHERE parent_id = ? AND kid_id = ?",
            (parent_id, kid_id),
        ).fetchone()
    return row is not None
