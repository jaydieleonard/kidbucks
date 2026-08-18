"""Test fixtures for KidBucks.

Every test runs against an isolated temporary SQLite database (never the real
data/kidbucks.db), with the read-cache cleared between tests so cached rows from
one test's database can't leak into another.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.pop("DATABASE_URL", None)  # force the SQLite backend for tests

import auth  # noqa: E402
import db  # noqa: E402
import seed  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point the DB at a throwaway file and clear caches around each test."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db._clear_read_cache()
    yield
    db._clear_read_cache()


@pytest.fixture
def seeded():
    """Seed the demo household and return its family id."""
    seed.main()
    return db.get_family_by_code(seed.FAMILY_CODE)["id"]


@pytest.fixture
def clean():
    """An empty family with just an admin parent; returns (family_id, mom_id)."""
    db.init_db()
    fam_id, _code = db.create_family("Test Family")
    h, s = auth.hash_secret("pw")
    mom_id = db.create_user(fam_id, "Mom", "parent", h, s, username="mom",
                            is_admin=True)
    return fam_id, mom_id
