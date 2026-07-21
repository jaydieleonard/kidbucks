"""KidBucks (K₿) — app entry point: login/register gate + role-based router.

Run with:  streamlit run Home.py

Everyone belongs to a family. Logged-out users enter a family code and then log
in (or register — creating a new family, or joining one by code). Once logged in
a sidebar menu is built for the user's role (kid / parent / admin) via
st.navigation, so kids never see parent or admin pages.

Page scripts live in views/ (NOT pages/, whose auto-discovery would show every
page to everyone and defeat the role gating).
"""

from __future__ import annotations

import os

import streamlit as st

import auth
import db

st.set_page_config(page_title="KidBucks", page_icon="🪙", layout="wide")

# Use a hosted Postgres (e.g. Neon) when a DATABASE_URL is provided via Streamlit
# secrets or the environment; otherwise fall back to a local SQLite file. Setting
# it before init_db() ensures db.py picks the right backend.
try:
    if not os.environ.get("DATABASE_URL") and "DATABASE_URL" in st.secrets:
        os.environ["DATABASE_URL"] = st.secrets["DATABASE_URL"]
except Exception:
    pass

db.init_db()

EMOJI_CHOICES = ["🙂", "😀", "😎", "🦄", "🐯", "🦖", "🚀", "⚽", "🎮", "🌟", "🐶", "🐱"]


# --- Login / register screen -----------------------------------------------

def _brand_header() -> None:
    st.markdown("# K₿ &nbsp;KidBucks")
    st.caption(
        "Behaviour · Understanding · Chores · Kindness · Savings — earn "
        f"KidBucks ({auth.BUCK}) for helping out."
    )


def _resolve_family_code() -> dict | None:
    """Family-code input (prefilled from ?family=CODE). Returns the family or None."""
    qp_family = st.query_params.get("family", "")
    if "login_family_code" not in st.session_state and qp_family:
        st.session_state["login_family_code"] = qp_family

    code = st.text_input(
        "Family code", key="login_family_code", placeholder="e.g. SMITH-7K2Q"
    )
    if not code.strip():
        st.info("Enter your family code to log in, or **Register** to create one.")
        return None
    family = db.get_family_by_code(code)
    if not family:
        st.error("No family found for that code. Check it, or register below.")
        return None
    st.success(f"Family: **{family['name']}**")
    return family


def _login_tab() -> None:
    st.subheader("Log in")
    family = _resolve_family_code()
    if not family:
        return

    role = st.radio("I am a…", ["Kid", "Parent"], horizontal=True, key="login_role")

    if role == "Kid":
        kids = [k["name"] for k in db.list_all_kids(family["id"])]
        if not kids:
            st.info("No kid accounts in this family yet. Register one below.")
            return
        with st.form("kid_login"):
            name = st.selectbox("Your name", kids)
            pin = st.text_input("PIN", type="password")
            if st.form_submit_button("Log in", width="stretch"):
                user = auth.authenticate_kid(family["id"], name, pin)
                if user:
                    auth.login(user)
                    st.rerun()
                else:
                    st.error("Wrong PIN. Try again.")
    else:
        with st.form("parent_login"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Log in", width="stretch"):
                user = auth.authenticate_parent(family["id"], username.strip(), password)
                if user:
                    auth.login(user)
                    st.rerun()
                else:
                    st.error("Wrong username or password.")


def _register_tab() -> None:
    st.subheader("Register")
    mode = st.radio(
        "What would you like to do?",
        ["Create a new family", "Join a family"],
        key="reg_mode",
    )
    if mode == "Create a new family":
        _create_family_form()
    else:
        _join_family_form()


def _create_family_form() -> None:
    st.caption("You'll become the **main parent (admin)** and get a code to invite others.")
    with st.form("create_family"):
        family_name = st.text_input("Family name", placeholder="e.g. The Smiths")
        st.markdown("**Your parent account**")
        name = st.text_input("Your display name")
        username = st.text_input("Username (for logging in)")
        emoji = st.selectbox("Avatar", EMOJI_CHOICES)
        password = st.text_input("Password", type="password")
        confirm = st.text_input("Confirm password", type="password")
        if st.form_submit_button("Create family", width="stretch"):
            if not family_name.strip() or not name.strip() or not username.strip() \
                    or not password:
                st.error("Family name, your name, username and password are required.")
            elif password != confirm:
                st.error("Passwords don't match.")
            else:
                fam_id, code = db.create_family(family_name.strip())
                secret_hash, salt = auth.hash_secret(password)
                uid = db.create_user(
                    fam_id, name.strip(), "parent", secret_hash, salt,
                    username=username.strip(), is_admin=True, emoji=emoji,
                )
                st.session_state["just_created_code"] = code
                auth.login(db.get_user(uid))
                st.rerun()


def _join_family_form() -> None:
    with st.form("join_family"):
        code = st.text_input("Family code", placeholder="e.g. SMITH-7K2Q")
        role = st.radio("Join as…", ["Parent", "Kid"], horizontal=True)
        name = st.text_input("Name")
        emoji = st.selectbox("Avatar", EMOJI_CHOICES)
        if role == "Parent":
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            confirm = st.text_input("Confirm password", type="password")
        else:
            username = None
            password = st.text_input("Choose a PIN (numbers)", type="password")
            confirm = st.text_input("Confirm PIN", type="password")
        if st.form_submit_button("Join family", width="stretch"):
            _do_join(code, role, name, emoji, username, password, confirm)


def _do_join(code, role, name, emoji, username, password, confirm) -> None:
    family = db.get_family_by_code(code) if code.strip() else None
    if not family:
        st.error("No family found for that code.")
        return
    if not name.strip() or not password:
        st.error("Name and password/PIN are required.")
        return
    if password != confirm:
        st.error("Password/PIN doesn't match its confirmation.")
        return
    fam_id = family["id"]
    if role == "Parent":
        if not username.strip():
            st.error("Username is required.")
            return
        if db.username_exists(fam_id, username.strip()):
            st.error("That username is already taken in this family.")
            return
        secret_hash, salt = auth.hash_secret(password)
        uid = db.create_user(
            fam_id, name.strip(), "parent", secret_hash, salt,
            username=username.strip(), is_admin=False, emoji=emoji,
        )
    else:
        if db.kid_name_exists(fam_id, name.strip()):
            st.error("A kid with that name already exists in this family.")
            return
        secret_hash, salt = auth.hash_secret(password)
        uid = db.create_user(fam_id, name.strip(), "kid", secret_hash, salt, emoji=emoji)
    auth.login(db.get_user(uid))
    st.rerun()


def _login_screen() -> None:
    _brand_header()
    st.divider()
    left, _ = st.columns([1, 1])
    with left:
        tab_login, tab_register = st.tabs(["Log in", "Register"])
        with tab_login:
            _login_tab()
        with tab_register:
            _register_tab()


# --- Logged-in router ------------------------------------------------------

def _build_navigation(user: dict):
    """Return an st.navigation object with pages for this user's role."""
    if user["role"] == "kid":
        pages = {
            "My KidBucks": [
                st.Page("views/wallet.py", title="My Wallet", icon="👛", default=True),
                st.Page("views/do_chores.py", title="Do Chores", icon="✅"),
                st.Page("views/redeem.py", title="Redeem", icon="🎁"),
            ]
        }
    else:  # parent (and admin, which is a parent with extra pages)
        pages = {
            "Parent": [
                st.Page("views/dashboard.py", title="Dashboard", icon="📊",
                        default=True),
                st.Page("views/approvals.py", title="Approvals", icon="🔔"),
                st.Page("views/manage_chores.py", title="Chores", icon="🧹"),
                st.Page("views/penalties.py", title="Penalties", icon="⚠️"),
                st.Page("views/family.py", title="Family", icon="👨‍👩‍👧‍👦"),
            ]
        }
        if user["is_admin"]:
            pages["Admin"] = [
                st.Page("views/redemption_options.py", title="Redemption Options",
                        icon="💱"),
            ]
    return st.navigation(pages)


def _sidebar_account(user: dict) -> None:
    with st.sidebar:
        st.markdown(f"### {user['emoji']} {user['name']}")
        role_label = "Admin parent" if user["is_admin"] else user["role"].capitalize()
        st.caption(role_label)
        st.caption(f"👨‍👩‍👧‍👦 {user['family_name']}  ·  `{user['family_code']}`")
        if st.button("Log out", width="stretch"):
            auth.logout()
            st.rerun()


# --- Main ------------------------------------------------------------------

user = auth.current_user()
if user is None:
    _login_screen()
else:
    _sidebar_account(user)
    _build_navigation(user).run()
