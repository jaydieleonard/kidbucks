"""Data access layer for KidBucks (K₿) — a family chores, rewards & wallet app.

Uses Python's built-in sqlite3 module, so there is nothing extra to install.
The database is a single file at data/kidbucks.db (created on first run).

Core design — the ledger is the single source of truth
------------------------------------------------------
A kid's wallet balance is NEVER stored directly. Every change (a chore payout,
a penalty, a redemption, a manual adjustment) is a row in the `transactions`
ledger: positive = bucks in, negative = bucks out. A balance is simply the SUM
of a kid's rows.

Families
--------
Everyone belongs to exactly one `family`. A family has a shareable `code`; other
parents and kids join by entering it. Usernames and kid names are unique WITHIN a
family (so every family can have its own "Mom"/"Dad"). Chores, penalties and
redemption rates are all scoped to a family.

Recurrence
----------
Chores are one-time or recurring (daily/weekly/monthly). Each submission stores a
`period_key` derived from the chore's recurrence at submit time, so "already done
this period" is a simple equality check. A chore flagged `shared` can be claimed
by only ONE kid in the family per period (first come).

Snapshots
---------
Chore/penalty/redemption records store the buck `value` at the moment they were
created, so later edits never rewrite already-approved history.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

# Resolve the DB path relative to THIS file so the app works no matter what
# directory Streamlit is launched from.
DB_PATH = Path(__file__).parent / "data" / "kidbucks.db"

# Transaction ledger categories.
TXN_CHORE = "chore"
TXN_PENALTY = "penalty"
TXN_REDEMPTION = "redemption"
TXN_ADJUSTMENT = "adjustment"
TXN_BONUS = "bonus"        # spontaneous reward for good behaviour
TXN_DEMERIT = "demerit"    # spontaneous one-off deduction

# Currency glyph for KidBucks (kept here too so db-layer logs can use it).
BUCK = "₿"

# Chore recurrence values and their human labels.
RECURRENCE_ONCE = "once"
RECURRENCE_OPTIONS = ["once", "daily", "weekly", "monthly"]
RECURRENCE_LABELS = {
    "once": "One-time",
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
}

# Family-code alphabet with ambiguous characters (I, L, O, 0, 1) removed.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# --- Starter content given to every brand-new family (tweak freely) --------
# Applied once when a family is created, so they aren't staring at blank Chores/
# Penalties/Redemption pages (and so redeeming + the pocket-money dashboard work
# out of the box). Families can edit/delete/add anything afterwards.
DEFAULT_CHORES = [
    # name, value, recurrence, shared, description
    ("Make your bed", 5, "daily", False, "Every morning."),
    ("Wash the dishes", 10, "daily", False, "After dinner."),
    ("Tidy your room", 15, "weekly", False, ""),
    ("Take out the bins", 20, "weekly", True, "Family task — whoever does it first!"),
]
DEFAULT_PENALTIES = [
    # name, value, description
    ("Back-talk", 10, "Being rude to a grown-up."),
    ("Missed homework", 15, "Homework not done on time."),
    ("Left a mess", 5, ""),
]
DEFAULT_REDEMPTIONS = [
    # name, unit, bucks_per_unit, per_child
    ("Pocket Money", "R", 10.0, True),    # 10 KidBucks = R1, per-child rates on
    ("Screen Time", "min", 2.0, False),   # 2 KidBucks = 1 minute
]


def seed_family_defaults(family_id: int, created_by: int | None = None) -> None:
    """Give a new family the starter chores, penalties and redemption options."""
    for name, value, recurrence, shared, desc in DEFAULT_CHORES:
        create_chore(family_id, name, value, recurrence, shared, desc,
                     created_by=created_by)
    for name, value, desc in DEFAULT_PENALTIES:
        create_penalty(family_id, name, value, desc, created_by=created_by)
    for name, unit, rate, per_child in DEFAULT_REDEMPTIONS:
        create_redemption_option(family_id, name, unit, rate, per_child)


# --- Database backend: SQLite locally, Postgres (Neon) when DATABASE_URL set --

def _database_url() -> str:
    """The Postgres connection string, if one is configured (else empty)."""
    return os.environ.get("DATABASE_URL", "").strip()


def is_postgres() -> bool:
    return bool(_database_url())


# --- Read-query caching (Streamlit only) -----------------------------------
# Read functions are cached briefly to avoid re-hitting the DB on every rerun.
# ANY write clears the cache (see _Conn.__exit__), so data is never stale beyond
# a completed change. No-ops when Streamlit isn't running (e.g. seed.py).
try:
    import streamlit as _st
except Exception:  # pragma: no cover
    _st = None

_CACHE_TTL_SECONDS = 10


def _cached(fn):
    if _st is None:
        return fn
    try:
        return _st.cache_data(ttl=_CACHE_TTL_SECONDS, show_spinner=False)(fn)
    except Exception:
        return fn


def _clear_read_cache() -> None:
    if _st is None:
        return
    try:
        _st.cache_data.clear()
    except Exception:
        pass


class _Row(dict):
    """A dict row that also supports positional access (row[0]), like sqlite3.Row.

    Lets the same query code run on both backends unchanged.
    """

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _pg_row_factory(cursor):
    from psycopg.rows import dict_row
    make_dict = dict_row(cursor)
    return lambda values: _Row(make_dict(values))


# A process-wide connection pool for Postgres, so we reuse warm connections
# instead of doing a TLS handshake to the remote DB on every query (the main
# cause of slow pages on Neon). Created lazily; falls back to direct connections
# if the pool library/creation fails.
_pg_pool = None
_pg_pool_failed = False
_pool_lock = threading.Lock()


def _get_pg_pool():
    global _pg_pool, _pg_pool_failed
    # Kill switch: set KIDBUCKS_DISABLE_POOL (env / Streamlit secret) to skip the
    # pool entirely and use direct connections — no code change needed.
    if os.environ.get("KIDBUCKS_DISABLE_POOL"):
        return None
    if _pg_pool is not None or _pg_pool_failed:
        return _pg_pool
    with _pool_lock:
        if _pg_pool is None and not _pg_pool_failed:
            try:
                from psycopg_pool import ConnectionPool
                _pg_pool = ConnectionPool(
                    _database_url(),
                    # min_size=0: connect lazily on demand (avoids a background
                    # worker that can stall app startup). check: validate/replace
                    # stale connections (Neon drops them when it auto-suspends).
                    min_size=0, max_size=5, timeout=6, max_idle=120,
                    # prepare_threshold=None disables prepared statements, which
                    # are incompatible with Neon's PgBouncer pooler endpoint.
                    kwargs={"autocommit": False, "row_factory": _pg_row_factory,
                            "prepare_threshold": None},
                    check=ConnectionPool.check_connection,
                    open=True,
                )
            except Exception:
                _pg_pool_failed = True
    return _pg_pool


def _disable_pg_pool() -> None:
    """Stop using the pool for the rest of this process (fall back to direct)."""
    global _pg_pool, _pg_pool_failed
    _pg_pool_failed = True
    pool, _pg_pool = _pg_pool, None
    if pool is not None:
        try:
            pool.close()
        except Exception:
            pass


class _Conn:
    """Uniform wrapper over sqlite3 / psycopg connections.

    Translates our SQLite-flavoured SQL to Postgres on the fly (placeholders,
    autoincrement, `INSERT OR IGNORE`, `COLLATE NOCASE`) so the rest of this
    module is written once. Commits on clean exit, rolls back on error, and
    clears the read cache whenever the block performed a write.
    """

    def __init__(self, raw, postgres: bool, pool=None):
        self._raw = raw
        self._pg = postgres
        self._pool = pool
        self._wrote = False

    def _tr(self, sql: str) -> str:
        if not self._pg:
            return sql
        sql = sql.replace(
            "INTEGER PRIMARY KEY AUTOINCREMENT",
            "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY",
        )
        sql = sql.replace("COLLATE NOCASE", "")
        if "INSERT OR IGNORE INTO" in sql:
            sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO")
            sql = sql + " ON CONFLICT DO NOTHING"
        sql = sql.replace("?", "%s")
        return sql

    def _note_write(self, sql: str) -> None:
        head = sql.strip().split(None, 1)
        if head and head[0].upper() in {
            "INSERT", "UPDATE", "DELETE", "REPLACE", "ALTER", "CREATE", "DROP",
        }:
            self._wrote = True

    def execute(self, sql, params=()):
        self._note_write(sql)
        return self._raw.execute(self._tr(sql), tuple(params))

    def executemany(self, sql, seq):
        self._wrote = True
        cur = self._raw.cursor()
        cur.executemany(self._tr(sql), [tuple(p) for p in seq])
        return cur

    def insert(self, sql, params=()):
        """Run an INSERT and return the new row's `id` (both backends)."""
        self._wrote = True
        if self._pg:
            cur = self._raw.execute(self._tr(sql) + " RETURNING id", tuple(params))
            return cur.fetchone()[0]
        cur = self._raw.execute(sql, tuple(params))
        return cur.lastrowid

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type:
                self._raw.rollback()
            else:
                self._raw.commit()
        finally:
            if self._pool is not None:
                try:
                    self._pool.putconn(self._raw)   # return to pool (stays warm)
                except Exception:
                    try:
                        self._raw.close()
                    except Exception:
                        pass
            else:
                self._raw.close()
        if exc_type is None and self._wrote:
            _clear_read_cache()
        return False


def get_connection() -> _Conn:
    """Open a connection. Rows behave like dicts (row["name"]) and tuples (row[0])."""
    if is_postgres():
        pool = _get_pg_pool()
        if pool is not None:
            try:
                raw = pool.getconn(timeout=6)
                return _Conn(raw, postgres=True, pool=pool)
            except Exception:
                # Pool can't serve a connection: stop using it and go direct so
                # the app keeps working (this is what ran before pooling).
                _disable_pg_pool()
        # Direct connection — the fallback whenever the pool is unavailable.
        # No connect_timeout: allow Neon's cold-start wake to take as long as it
        # needs (matching the behaviour that worked before pooling).
        import psycopg
        raw = psycopg.connect(
            _database_url(), autocommit=False, row_factory=_pg_row_factory,
            prepare_threshold=None,
        )
        return _Conn(raw, postgres=True)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(DB_PATH)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return _Conn(raw, postgres=False)


def init_db() -> None:
    """Create the tables & indexes if they do not exist yet."""
    with get_connection() as conn:
        # A household. Everyone belongs to one; `code` is shared to invite others.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS families (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    NOT NULL,
                code       TEXT    NOT NULL UNIQUE,
                created_at TEXT    NOT NULL
            )
            """
        )
        # People: parents and kids, distinguished by `role`. Parents log in with
        # username + password; kids with name + PIN. `is_admin` marks the family's
        # "main parent" (controls conversion rates & user management).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                family_id   INTEGER NOT NULL REFERENCES families(id) ON DELETE CASCADE,
                name        TEXT    NOT NULL,
                role        TEXT    NOT NULL CHECK (role IN ('kid', 'parent')),
                is_admin    INTEGER NOT NULL DEFAULT 0,
                username    TEXT,
                secret_hash TEXT    NOT NULL,
                salt        TEXT    NOT NULL,
                emoji       TEXT    NOT NULL DEFAULT '🙂',
                created_at  TEXT    NOT NULL
            )
            """
        )
        # Names/usernames unique within a family (NULL usernames stay distinct, so
        # many kids with no username coexist fine).
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_family_username "
            "ON users(family_id, username)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_kid_family_name "
            "ON users(family_id, name) WHERE role = 'kid'"
        )
        # Many-to-many: a kid can be linked to one or several parents.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS parent_kid (
                parent_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kid_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                PRIMARY KEY (parent_id, kid_id)
            )
            """
        )
        # Chore templates (per family). `recurrence` controls how often a chore can
        # be earned; `shared` = only one kid in the family may claim it per period.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chores (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                family_id   INTEGER NOT NULL REFERENCES families(id) ON DELETE CASCADE,
                name        TEXT    NOT NULL,
                description TEXT    NOT NULL DEFAULT '',
                value       INTEGER NOT NULL,
                recurrence  TEXT    NOT NULL DEFAULT 'once',
                shared      INTEGER NOT NULL DEFAULT 0,
                active      INTEGER NOT NULL DEFAULT 1,
                created_by  INTEGER REFERENCES users(id),
                created_at  TEXT    NOT NULL
            )
            """
        )
        # A kid claiming a chore. `period_key` snapshots which recurrence window the
        # claim belongs to. On approval a transaction is written for `value`.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chore_submissions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                kid_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                chore_id     INTEGER NOT NULL REFERENCES chores(id) ON DELETE CASCADE,
                status       TEXT    NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending', 'approved', 'rejected')),
                value        INTEGER NOT NULL,
                period_key   TEXT    NOT NULL DEFAULT '',
                note         TEXT    NOT NULL DEFAULT '',
                submitted_at TEXT    NOT NULL,
                reviewed_by  INTEGER REFERENCES users(id),
                reviewed_at  TEXT
            )
            """
        )
        # The wallet ledger — the single source of truth for every balance.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                kid_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                amount     INTEGER NOT NULL,
                type       TEXT    NOT NULL,
                reason     TEXT    NOT NULL DEFAULT '',
                ref_id     INTEGER,
                created_at TEXT    NOT NULL
            )
            """
        )
        # Penalty templates parents can apply for misbehaviour (per family).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS penalties (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                family_id   INTEGER NOT NULL REFERENCES families(id) ON DELETE CASCADE,
                name        TEXT    NOT NULL,
                description TEXT    NOT NULL DEFAULT '',
                value       INTEGER NOT NULL,
                active      INTEGER NOT NULL DEFAULT 1,
                created_by  INTEGER REFERENCES users(id),
                created_at  TEXT    NOT NULL
            )
            """
        )
        # A record of a penalty being handed to a kid (writes a -value txn).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS penalty_applications (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                kid_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                penalty_id INTEGER REFERENCES penalties(id),
                value      INTEGER NOT NULL,
                note       TEXT    NOT NULL DEFAULT '',
                applied_by INTEGER REFERENCES users(id),
                applied_at TEXT    NOT NULL
            )
            """
        )
        # Ways to spend bucks (per family). Adding a row surfaces a new redemption
        # type on the Redeem page with no code change. Rate = bucks_per_unit.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS redemption_options (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                family_id      INTEGER NOT NULL REFERENCES families(id) ON DELETE CASCADE,
                name           TEXT    NOT NULL,
                unit           TEXT    NOT NULL,
                bucks_per_unit REAL    NOT NULL,
                per_child      INTEGER NOT NULL DEFAULT 0,
                active         INTEGER NOT NULL DEFAULT 1,
                created_at     TEXT    NOT NULL
            )
            """
        )
        # Per-child conversion-rate overrides. If a (option, kid) row exists it
        # wins over the option's default bucks_per_unit for that kid.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS redemption_rates (
                option_id      INTEGER NOT NULL REFERENCES redemption_options(id)
                                   ON DELETE CASCADE,
                kid_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                bucks_per_unit REAL    NOT NULL,
                PRIMARY KEY (option_id, kid_id)
            )
            """
        )
        # A kid's request to convert bucks; awaits parent approval.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS redemption_requests (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                kid_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                option_id    INTEGER REFERENCES redemption_options(id),
                option_name  TEXT    NOT NULL,
                units        REAL    NOT NULL,
                unit_label   TEXT    NOT NULL,
                bucks_spent  INTEGER NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending', 'approved', 'rejected')),
                requested_at TEXT    NOT NULL,
                reviewed_by  INTEGER REFERENCES users(id),
                reviewed_at  TEXT,
                paid         INTEGER NOT NULL DEFAULT 0,
                paid_at      TEXT
            )
            """
        )

        # Audit trail for chore changes: who created / edited / archived /
        # restored a chore, what changed, and when. Names are snapshotted so the
        # log stays readable regardless of later edits.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chore_audit (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                family_id  INTEGER NOT NULL REFERENCES families(id) ON DELETE CASCADE,
                chore_id   INTEGER,
                chore_name TEXT    NOT NULL DEFAULT '',
                actor_id   INTEGER,
                actor_name TEXT    NOT NULL DEFAULT '',
                action     TEXT    NOT NULL,
                detail     TEXT    NOT NULL DEFAULT '',
                created_at TEXT    NOT NULL
            )
            """
        )

        # Lightweight migrations so an already-deployed database (e.g. Neon) gains
        # newer columns without a manual step. CREATE TABLE IF NOT EXISTS never
        # alters an existing table, so we add any missing columns here.
        _add_column_if_missing(conn, "chores", "recurrence",
                               "TEXT NOT NULL DEFAULT 'once'")
        _add_column_if_missing(conn, "chores", "shared", "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "chore_submissions", "period_key",
                               "TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "redemption_options", "per_child",
                               "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "redemption_requests", "paid",
                               "INTEGER NOT NULL DEFAULT 0")
        _add_column_if_missing(conn, "redemption_requests", "paid_at", "TEXT")
        _add_column_if_missing(conn, "users", "last_seen_at", "TEXT")


def _add_column_if_missing(conn, table: str, column: str, coldef: str) -> None:
    """Add a column only if the table doesn't already have it (both backends)."""
    if is_postgres():
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coldef}")
    else:
        existing = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# Units that are written before the amount (e.g. Rand -> "R1", not "1 R").
# Anything else (minutes, points, …) is written after it ("30 min").
_CURRENCY_UNITS = {"r", "$", "£", "€", "¥"}


def fmt_units(amount, unit: str) -> str:
    """Format a redemption quantity with its unit the natural way.

    Currency symbols prefix the amount ('R', '$' -> 'R5', '$5'); everything else
    is a suffix ('30 min'). Whole numbers show without a trailing '.0'.
    """
    a = f"{amount:g}"
    u = (unit or "").strip()
    if u.lower() in _CURRENCY_UNITS:
        return f"{u}{a}"
    return f"{a} {u}"


# --- Families --------------------------------------------------------------

def _slug(name: str) -> str:
    letters = "".join(ch for ch in name.upper() if ch.isalnum())
    return letters[:6] or "FAM"


def _gen_family_code(name: str) -> str:
    """A shareable code like SMITH-7K2Q, guaranteed unique."""
    slug = _slug(name)
    for _ in range(50):
        suffix = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
        code = f"{slug}-{suffix}"
        if get_family_by_code(code) is None:
            return code
    return f"{slug}-{secrets.token_hex(3).upper()}"


def create_family(name: str) -> tuple[int, str]:
    """Create a family; returns (family_id, code)."""
    code = _gen_family_code(name)
    with get_connection() as conn:
        fam_id = conn.insert(
            "INSERT INTO families (name, code, created_at) VALUES (?, ?, ?)",
            (name, code, _now()),
        )
        return fam_id, code


@_cached
def get_family(family_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM families WHERE id = ?", (family_id,)
        ).fetchone()
    return dict(row) if row else None


@_cached
def get_family_by_code(code: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM families WHERE code = ?", (code.strip().upper(),)
        ).fetchone()
    return dict(row) if row else None


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
def get_user(user_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_parent_by_username(family_id: int, username: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE role = 'parent' AND family_id = ? "
            "AND username = ? COLLATE NOCASE",
            (family_id, username),
        ).fetchone()
    return dict(row) if row else None


def get_kid_by_name(family_id: int, name: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE role = 'kid' AND family_id = ? "
            "AND name = ? COLLATE NOCASE",
            (family_id, name),
        ).fetchone()
    return dict(row) if row else None


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
def list_parents(family_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE role = 'parent' AND family_id = ? "
            "ORDER BY name COLLATE NOCASE",
            (family_id,),
        ).fetchall()
    return [dict(r) for r in rows]


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


# --- Wallet / transactions -------------------------------------------------

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


# --- Chores ----------------------------------------------------------------

def _period_key(recurrence: str, dt: datetime) -> str:
    """Which recurrence window `dt` falls in. '' means the chore is one-time."""
    if recurrence == "daily":
        return dt.strftime("%Y-%m-%d")
    if recurrence == "weekly":
        return dt.strftime("%G-W%V")   # ISO year + ISO week (Mon-Sun)
    if recurrence == "monthly":
        return dt.strftime("%Y-%m")
    return ""


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


def get_chore(chore_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM chores WHERE id = ?", (chore_id,)).fetchone()
    return dict(row) if row else None


@_cached
def list_chores(family_id: int, active_only: bool = False) -> list[dict]:
    query = "SELECT * FROM chores WHERE family_id = ?"
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY active DESC, name COLLATE NOCASE"
    with get_connection() as conn:
        rows = conn.execute(query, (family_id,)).fetchall()
    return [dict(r) for r in rows]


@_cached
def chore_audit_log(family_id: int, limit: int = 50) -> list[dict]:
    """Who changed chores in this family (newest first)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM chore_audit WHERE family_id = ? ORDER BY id DESC LIMIT ?",
            (family_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


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


# --- Penalties -------------------------------------------------------------

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


# --- Redemption options (rates) & requests ---------------------------------

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


def _redemption_cost(bucks_per_unit: float, units: float) -> int:
    """Bucks needed for `units`, rounded up so kids never underpay."""
    import math

    return int(math.ceil(bucks_per_unit * units))


def redemption_cost(bucks_per_unit: float, units: float) -> int:
    """Public helper so pages can preview a cost before submitting."""
    return _redemption_cost(bucks_per_unit, units)


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


# --- Aggregate counts ------------------------------------------------------

@_cached
def outstanding_approvals_for_parent(parent_id: int) -> int:
    with get_connection() as conn:
        chores = conn.execute(
            """
            SELECT COUNT(*) FROM chore_submissions s
              JOIN parent_kid pk ON pk.kid_id = s.kid_id
             WHERE pk.parent_id = ? AND s.status = 'pending'
            """,
            (parent_id,),
        ).fetchone()[0]
        redemptions = conn.execute(
            """
            SELECT COUNT(*) FROM redemption_requests r
              JOIN parent_kid pk ON pk.kid_id = r.kid_id
             WHERE pk.parent_id = ? AND r.status = 'pending'
            """,
            (parent_id,),
        ).fetchone()[0]
    return chores + redemptions


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


def is_currency_unit(unit: str) -> bool:
    return (unit or "").strip().lower() in _CURRENCY_UNITS


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
