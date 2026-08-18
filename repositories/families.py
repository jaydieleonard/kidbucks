"""Families repository: the household grouping and its shareable join code.

Everyone belongs to exactly one family. A family has a `code` (e.g. SMITH-7K2Q)
that other parents and kids enter to join. Usernames and kid names are unique
WITHIN a family, so every family can have its own "Mom"/"Dad".
"""

from __future__ import annotations

import secrets

from config import CODE_ALPHABET
from engine import _cached, _now, get_connection
from models import Family


def _slug(name: str) -> str:
    letters = "".join(ch for ch in name.upper() if ch.isalnum())
    return letters[:6] or "FAM"


def _gen_family_code(name: str) -> str:
    """A shareable code like SMITH-7K2Q, guaranteed unique."""
    slug = _slug(name)
    for _ in range(50):
        suffix = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
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
def get_family(family_id: int) -> Family | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM families WHERE id = ?", (family_id,)
        ).fetchone()
    return Family.from_row(row)


@_cached
def get_family_by_code(code: str) -> Family | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM families WHERE code = ?", (code.strip().upper(),)
        ).fetchone()
    return Family.from_row(row)
