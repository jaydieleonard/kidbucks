"""Typed domain models: attribute access AND dict compatibility (drop-in)."""

from __future__ import annotations

import db
from models import Chore, Family, User


def test_get_user_returns_typed_but_dict_compatible(seeded):
    fam = seeded
    u = db.get_kid_by_name(fam, "Ava")
    assert isinstance(u, User)
    # attribute access (the new way)
    assert u.name == "Ava" and u.role == "kid"
    # dict access (the old way) still works everywhere it's used
    assert u["name"] == "Ava"
    assert u.get("last_seen_at", "x") in (None, u.last_seen_at)
    assert "family_id" in u
    assert dict(u)["emoji"] == u.emoji


def test_get_family_and_chore_typed(seeded):
    fam = seeded
    f = db.get_family(fam)
    assert isinstance(f, Family) and f.code and f["name"]
    chore = db.list_chores(fam, active_only=True)[0]
    assert isinstance(chore, Chore)
    assert chore.value == chore["value"] and isinstance(chore.value, int)


def test_missing_row_is_none(clean):
    assert Family.from_row(None) is None
    assert db.get_family(999999) is None  # table exists (clean), row doesn't


def test_list_parents_are_users(seeded):
    fam = seeded
    parents = db.list_parents(fam)
    assert parents and all(isinstance(p, User) for p in parents)
    assert {p.role for p in parents} == {"parent"}
