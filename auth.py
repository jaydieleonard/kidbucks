"""Authentication & session helpers for KidBucks.

Secrets (parent passwords and kid PINs) are hashed with PBKDF2-HMAC-SHA256 and a
per-user random salt, using only the standard library — no extra dependency.

NOTE: this is family-scale auth, not hardened production security. It keeps
secrets out of plaintext, but the app is meant to run on a home machine/network,
not exposed to the open internet.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

import streamlit as st

import db

_PBKDF2_ROUNDS = 200_000

# Currency glyph shown throughout the app (KidBucks = ₿).
BUCK = "₿"


def fmt_bucks(amount) -> str:
    """Render an amount of KidBucks, e.g. 125 -> '125 ₿'."""
    return f"{amount:,} {BUCK}"


# --- Family invite helpers -------------------------------------------------

def invite_message(family_name: str, code: str, join_url: str | None = None) -> str:
    """A ready-to-send message inviting someone to join a family."""
    msg = (
        f"Join our family \"{family_name}\" on KidBucks! 🪙\n"
        f"Open the app and enter family code: {code}"
    )
    if join_url:
        msg += f"\nOr tap this link: {join_url}"
    return msg


def invite_links(family_name: str, code: str, join_url: str | None = None) -> dict:
    """Deep-links that open the device's messaging apps with the invite prefilled."""
    from urllib.parse import quote

    body = quote(invite_message(family_name, code, join_url))
    subject = quote(f"Join {family_name} on KidBucks")
    return {
        "sms": f"sms:?&body={body}",
        "whatsapp": f"https://wa.me/?text={body}",
        "email": f"mailto:?subject={subject}&body={body}",
    }


# --- Hashing ---------------------------------------------------------------

def hash_secret(secret: str, salt: str | None = None) -> tuple[str, str]:
    """Return (hash_hex, salt_hex). Generates a salt when one isn't supplied."""
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ROUNDS
    )
    return digest.hex(), salt


def verify_secret(secret: str, salt: str, expected_hash: str) -> bool:
    calculated, _ = hash_secret(secret, salt)
    return hmac.compare_digest(calculated, expected_hash)


# --- Session ---------------------------------------------------------------

def _to_session_user(row: dict) -> dict:
    """The subset of user fields we keep in the session (never the secret)."""
    family = db.get_family(row["family_id"])
    return {
        "id": row["id"],
        "family_id": row["family_id"],
        "family_name": family["name"] if family else "",
        "family_code": family["code"] if family else "",
        "name": row["name"],
        "role": row["role"],
        "is_admin": bool(row["is_admin"]),
        "emoji": row["emoji"],
    }


def login(user_row: dict) -> None:
    st.session_state["user"] = _to_session_user(user_row)


def logout() -> None:
    st.session_state.pop("user", None)


def current_user() -> dict | None:
    return st.session_state.get("user")


def is_admin() -> bool:
    user = current_user()
    return bool(user and user["is_admin"])


def authenticate_parent(family_id: int, username: str, password: str) -> dict | None:
    row = db.get_parent_by_username(family_id, username)
    if row and verify_secret(password, row["salt"], row["secret_hash"]):
        return row
    return None


def authenticate_kid(family_id: int, name: str, pin: str) -> dict | None:
    row = db.get_kid_by_name(family_id, name)
    if row and verify_secret(pin, row["salt"], row["secret_hash"]):
        return row
    return None


# --- Page guards -----------------------------------------------------------

def require(*roles: str, admin: bool = False) -> dict:
    """Guard a page. Stops rendering if the user isn't allowed.

    `require("parent")` allows any parent; `require(admin=True)` requires the
    main (admin) parent; `require()` just requires being logged in.
    """
    user = current_user()
    if user is None:
        st.error("Please log in first.")
        st.stop()
    if roles and user["role"] not in roles:
        st.error("You don't have access to this page.")
        st.stop()
    if admin and not user["is_admin"]:
        st.error("Only the main parent (admin) can open this page.")
        st.stop()
    return user
