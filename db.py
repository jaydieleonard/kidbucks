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

# db.py is now a thin FACADE. The real work lives in focused modules — engine
# (connection/pool/cache), config (constants), formatting (display), models
# (typed rows) and the repositories/ package (one per aggregate). Everything is
# re-imported here so existing callers keep using the flat `db.*` interface
# unchanged; the names below are deliberately re-exported even where this module
# no longer uses them itself. db.py itself now owns only schema creation,
# lightweight column migrations, family seeding and cross-aggregate counts.
from config import (
    BUCK, CODE_ALPHABET, DEFAULT_CHORES, DEFAULT_PENALTIES, DEFAULT_REDEMPTIONS,
    RECURRENCE_LABELS, RECURRENCE_ONCE, RECURRENCE_OPTIONS,
    TXN_ADJUSTMENT, TXN_BONUS, TXN_CHORE, TXN_DEMERIT, TXN_PENALTY, TXN_REDEMPTION,
)
from formatting import fmt_dt, fmt_units, is_currency_unit
from models import Chore, Family, User
from engine import (
    DB_PATH, _cached, _clear_read_cache, _now, get_connection, is_postgres,
)
from repositories.audit import _log_chore_audit, chore_audit_log
from repositories.penalties import (
    apply_penalty, create_penalty, list_penalties, set_penalty_active,
    update_penalty,
)
from repositories.ledger import (
    add_transaction, earned_this_month, get_balance, kid_summary,
    recent_transactions,
)
from repositories.redemptions import (
    approve_redemption, clear_kid_rate, create_redemption_option, current_month,
    effective_rate, get_kid_rate_overrides, get_redemption_option, kid_redemptions,
    list_redemption_options, mark_redemption_paid, month_label,
    pending_redemptions_for_parent, pocket_money_month_total,
    pocket_money_monthly_history, pocket_money_option, pocket_money_redemptions,
    redemption_cost, reject_redemption, request_redemption, set_kid_rate,
    set_redemption_option_active, update_redemption_option, update_redemption_rate,
)
from repositories.families import (
    create_family, get_family, get_family_by_code,
)
from repositories.users import (
    count_parents, create_user, delete_user, get_kid_by_name, get_kid_parent_ids,
    get_parent_by_username, get_user, kid_name_exists, link_parent_kid,
    list_all_kids, list_kids_for_parent, list_parents, parent_can_see_kid,
    reset_secret, set_admin, set_kid_parents, touch_last_seen, unlink_parent_kid,
    update_user_profile, username_exists,
)
from repositories.chores import (
    _period_key, approve_chore_submission, available_chores_for_kid, create_chore,
    get_chore, kid_reviewed_items, kid_submissions, list_chores,
    pending_chore_submissions_for_parent, reject_chore_submission, set_chore_active,
    submit_chore, update_chore,
)


def seed_family_defaults(family_id: int, created_by: int | None = None) -> None:
    """Give a new family the starter chores, penalties and redemption options."""
    for name, value, recurrence, shared, desc in DEFAULT_CHORES:
        create_chore(family_id, name, value, recurrence, shared, desc,
                     created_by=created_by)
    for name, value, desc in DEFAULT_PENALTIES:
        create_penalty(family_id, name, value, desc, created_by=created_by)
    for name, unit, rate, per_child in DEFAULT_REDEMPTIONS:
        create_redemption_option(family_id, name, unit, rate, per_child)


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

    # Column migrations run AFTER the create block, each in its OWN transaction,
    # so a hiccup on one can't poison the others or crash startup (on Postgres a
    # failed statement aborts the whole surrounding transaction).
    _run_column_migrations()


# Columns added to already-existing databases (e.g. Neon). CREATE TABLE IF NOT
# EXISTS never alters an existing table, so any newer column is added here.
_COLUMN_MIGRATIONS = [
    ("chores", "recurrence", "TEXT NOT NULL DEFAULT 'once'"),
    ("chores", "shared", "INTEGER NOT NULL DEFAULT 0"),
    ("chore_submissions", "period_key", "TEXT NOT NULL DEFAULT ''"),
    ("redemption_options", "per_child", "INTEGER NOT NULL DEFAULT 0"),
    ("redemption_requests", "paid", "INTEGER NOT NULL DEFAULT 0"),
    ("redemption_requests", "paid_at", "TEXT"),
    ("users", "last_seen_at", "TEXT"),
]


def _run_column_migrations() -> None:
    for table, column, coldef in _COLUMN_MIGRATIONS:
        try:
            with get_connection() as conn:
                _add_column_if_missing(conn, table, column, coldef)
        except Exception:
            pass  # best-effort; never let a migration crash startup


def _add_column_if_missing(conn, table: str, column: str, coldef: str) -> None:
    """Add a column only if the table doesn't already have it (both backends).

    Checks the catalog first (rather than relying on ADD COLUMN IF NOT EXISTS)
    so no ALTER is issued at all when the column already exists.
    """
    if is_postgres():
        exists = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = ?",
            (table, column),
        ).fetchone()
        if not exists:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
    else:
        existing = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")


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
