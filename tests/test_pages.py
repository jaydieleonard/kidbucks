"""Smoke tests: every page renders for its role, and role-gating holds.

Uses Streamlit's AppTest to run each view headlessly with a logged-in user.
"""

from __future__ import annotations

import pathlib

import pytest
from streamlit.testing.v1 import AppTest

import auth
import db

ROOT = pathlib.Path(__file__).resolve().parent.parent

KID_PAGES = ["wallet", "do_chores", "redeem", "account"]
PARENT_PAGES = ["dashboard", "approvals", "manage_chores", "penalties",
                "pocket_money", "family", "account"]
ADMIN_PAGES = ["redemption_options"]


def _run(page: str, user: dict):
    at = AppTest.from_file(str(ROOT / "views" / f"{page}.py"), default_timeout=30)
    at.session_state["user"] = user
    at.run()
    return at


def _su(fam, getter, key):
    return auth._to_session_user(getter(fam, key))


@pytest.mark.parametrize("page", KID_PAGES)
def test_kid_pages_render(seeded, page):
    kid = _su(seeded, db.get_kid_by_name, "Ava")
    at = _run(page, kid)
    assert not at.exception, f"{page} raised: {list(at.exception)}"


@pytest.mark.parametrize("page", PARENT_PAGES + ADMIN_PAGES)
def test_admin_pages_render(seeded, page):
    admin = _su(seeded, db.get_parent_by_username, "mom")
    at = _run(page, admin)
    assert not at.exception, f"{page} raised: {list(at.exception)}"


def test_kid_blocked_from_parent_page(seeded):
    kid = _su(seeded, db.get_kid_by_name, "Ava")
    at = _run("dashboard", kid)
    assert any("access" in e.value.lower() for e in at.error)


def test_non_admin_blocked_from_admin_page(seeded):
    dad = _su(seeded, db.get_parent_by_username, "dad")
    at = _run("redemption_options", dad)
    assert any("admin" in e.value.lower() for e in at.error)
