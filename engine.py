"""Database engine: the infrastructure layer under the repositories.

Owns everything about *how* we talk to storage — connection, pooling, the
SQLite/Postgres dialect wrapper, and the read-query cache — with no knowledge of
the domain (families, chores, ledger…). db.py and the repository modules import
from here; this module imports nothing from them.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

# Resolve the DB path relative to THIS file (same directory as the app).
DB_PATH = Path(__file__).parent / "data" / "kidbucks.db"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- Backend selection: SQLite locally, Postgres (Neon) when DATABASE_URL set --

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
    autoincrement, `INSERT OR IGNORE`, `COLLATE NOCASE`) so the rest of the code
    is written once. Commits on clean exit, rolls back on error, and clears the
    read cache whenever the block performed a write.
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
