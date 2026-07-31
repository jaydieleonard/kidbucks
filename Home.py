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
import streamlit.components.v1 as components

import auth
import db

GA_MEASUREMENT_ID = "G-CTJJNPW96R"

st.set_page_config(page_title="KidBucks", page_icon="🪙", layout="wide")


def _inject_mobile_css() -> None:
    """Make the layout comfortable on a phone: reclaim padding, stack column
    rows vertically (so nothing is cramped) and use full-width tap targets.
    Only applies at phone widths; desktop is unchanged."""
    st.markdown(
        """
        <style>
        @media (max-width: 480px) {
          .block-container {
            padding-top: 3rem !important;
            padding-left: 0.9rem !important;
            padding-right: 0.9rem !important;
          }
          /* Stack any row of columns vertically instead of squishing them */
          div[data-testid="stHorizontalBlock"] {
            flex-wrap: wrap !important;
            gap: 0.5rem !important;
          }
          div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"],
          div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
            flex: 1 1 100% !important;
            width: 100% !important;
          }
          /* Comfortable, full-width buttons */
          .stButton > button, .stFormSubmitButton > button {
            width: 100%;
            min-height: 2.6rem;
          }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


_inject_mobile_css()


def _inject_google_analytics() -> None:
    """Load the Google Analytics gtag.js tag into the live page.

    Streamlit doesn't expose the page <head>, and patching the served
    index.html does NOT work on Streamlit Community Cloud (its static shell is
    read-only / pre-built). So we inject the tag with JavaScript from a hidden
    0-height component into the parent document, which is same-origin on
    Streamlit. This entry script runs for every view, so it covers every page;
    the guard means it loads only once.

    Verify with GA → Realtime (or the Tag Assistant browser extension). GA's
    server-side "check installation" cannot see JS-injected tags and will keep
    reporting "not detected" — that's a false negative on Streamlit, not a fault.
    """
    components.html(
        f"""
        <script>
        (function () {{
          try {{
            var p = window.parent, d = p.document;
            if (d.getElementById('ga-gtag-js')) return;   // already loaded
            var s = d.createElement('script');
            s.id = 'ga-gtag-js';
            s.async = true;
            s.src = 'https://www.googletagmanager.com/gtag/js?id={GA_MEASUREMENT_ID}';
            d.head.appendChild(s);
            p.dataLayer = p.dataLayer || [];
            function gtag(){{ p.dataLayer.push(arguments); }}
            p.gtag = gtag;
            gtag('js', new Date());
            gtag('config', '{GA_MEASUREMENT_ID}');
          }} catch (e) {{ /* analytics is best-effort */ }}
        }})();
        </script>
        """,
        height=0,
    )


_inject_google_analytics()

# Use a hosted Postgres (e.g. Neon) when a DATABASE_URL is provided via Streamlit
# secrets or the environment; otherwise fall back to a local SQLite file. Setting
# it before init_db() ensures db.py picks the right backend.
try:
    for _key in ("DATABASE_URL", "KIDBUCKS_SECRET"):
        if not os.environ.get(_key) and _key in st.secrets:
            os.environ[_key] = st.secrets[_key]
except Exception:
    pass

db.init_db()

EMOJI_CHOICES = ["🙂", "😀", "😎", "🦄", "🐯", "🦖", "🚀", "⚽", "🎮", "🌟", "🐶", "🐱"]

# --- Persistent "stay logged in" via a real browser cookie -----------------
# Uses a proper cookie component (reliable on Streamlit Cloud, unlike the
# sandboxed-frame trick). Everything degrades to normal login if the library or
# browser cookie is unavailable.
try:
    from streamlit_cookies_controller import CookieController
    _cookies = CookieController()
except Exception:
    _cookies = None

AUTH_COOKIE = "kidbucks_auth"
FAMILY_COOKIE = "kidbucks_family"


def _cookie_get(name: str):
    if _cookies is None:
        return None
    try:
        return _cookies.get(name)
    except Exception:
        return None


def _cookie_set(name: str, value: str, max_age: int) -> None:
    if _cookies is None:
        return
    try:
        _cookies.set(name, value, max_age=max_age, same_site="lax")
    except Exception:
        pass


def _cookie_del(name: str) -> None:
    if _cookies is None:
        return
    try:
        _cookies.remove(name)
    except Exception:
        pass


def _refresh_cookies(su: dict) -> None:
    """(Re)write the login + family cookies, extending their lifetime on use."""
    _cookie_set(AUTH_COOKIE, auth.make_login_token(su["id"], su["family_id"]),
                auth.LOGIN_TOKEN_TTL_SECONDS)
    if su.get("family_code"):
        _cookie_set(FAMILY_COOKIE, su["family_code"], auth.LOGIN_TOKEN_TTL_SECONDS)


def _finish_login(row: dict) -> None:
    """Log a user in and remember them on this device for next time."""
    auth.login(row)
    _refresh_cookies(auth.current_user())
    st.session_state["_cookies_fresh"] = True
    st.rerun()


def _try_cookie_login() -> None:
    """Silently log in from the saved cookie, if it's present and valid."""
    token = _cookie_get(AUTH_COOKIE)
    if not token:
        return
    parsed = auth.read_login_token(token)
    if not parsed:
        return
    uid, fid = parsed
    row = db.get_user(uid)
    if row and row["family_id"] == fid:
        auth.login(row)


# --- Login / register screen -----------------------------------------------

def _brand_header() -> None:
    st.markdown("# K₿ &nbsp;KidBucks")
    st.caption(
        "Behaviour · Understanding · Chores · Kindness · Savings — earn "
        f"KidBucks ({auth.BUCK}) for helping out."
    )


def _persist_family(code: str) -> None:
    """Remember this family on the device so the code isn't retyped next visit."""
    _cookie_set(FAMILY_COOKIE, code, auth.LOGIN_TOKEN_TTL_SECONDS)


def _forget_family() -> None:
    _cookie_del(FAMILY_COOKIE)


def _remembered_code() -> str:
    """The family code saved on this device from a previous visit (or '')."""
    return _cookie_get(FAMILY_COOKIE) or ""


def _resolve_family() -> dict | None:
    """Work out which family we're logging into WITHOUT retyping the code each
    time. Priority: URL ?family= → this session → this device (cookie). Kids can
    just bookmark the family link (or add it to their home screen) and land
    straight on the name + PIN step."""
    code = (
        st.query_params.get("family", "").strip()
        or st.session_state.get("family_code", "")
        or _remembered_code()
    )
    family = db.get_family_by_code(code) if code else None

    if family:
        # Remember it everywhere so refreshes and future visits skip the code.
        st.session_state["family_code"] = family["code"]
        if st.query_params.get("family") != family["code"]:
            st.query_params["family"] = family["code"]
        _persist_family(family["code"])
        st.success(f"👨‍👩‍👧‍👦 Family: **{family['name']}**")
        if st.button("Not your family? Switch", key="switch_family"):
            _forget_family()
            st.session_state.pop("family_code", None)
            st.session_state.pop("family_code_entry", None)
            try:
                del st.query_params["family"]
            except Exception:
                st.query_params.clear()
            st.rerun()
        return family

    # First time on this device — ask for the code once, then remember it.
    entered = st.text_input(
        "Family code", key="family_code_entry", placeholder="e.g. SMITH-7K2Q",
        help="Ask a parent for your family code. You only enter it once on this "
             "device — after that it's remembered.",
    ).strip()
    if entered:
        fam = db.get_family_by_code(entered)
        if fam:
            st.session_state["family_code"] = fam["code"]
            st.query_params["family"] = fam["code"]
            _persist_family(fam["code"])
            st.rerun()
        st.error("No family found for that code. Check it, or register below.")
    else:
        st.info("Enter your family code once — this device will remember it next time.")
    return None


def _login_tab() -> None:
    st.subheader("Log in")
    family = _resolve_family()
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
                    _finish_login(user)
                else:
                    st.error("Wrong PIN. Try again.")
    else:
        parents = db.list_parents(family["id"])
        if not parents:
            st.info("No parent accounts in this family yet. Register one below.")
            return
        with st.form("parent_login"):
            parent_by_label = {f"{p['emoji']} {p['name']}": p for p in parents}
            picked = st.selectbox("Your name", list(parent_by_label.keys()))
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Log in", width="stretch"):
                parent = parent_by_label[picked]
                user = auth.authenticate_parent(
                    family["id"], parent["username"], password
                )
                if user:
                    _finish_login(user)
                else:
                    st.error("Wrong password. Try again.")


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
                _finish_login(db.get_user(uid))


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
    _finish_login(db.get_user(uid))


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
        with st.expander("📲 Add KidBucks to your iPhone home screen"):
            st.markdown(
                "1. Open this page in **Safari**.\n"
                "2. Tap the **Share** button (a square with an arrow).\n"
                "3. Choose **Add to Home Screen** → **Add**.\n\n"
                "The icon remembers your family, so next time just tap your name "
                "and PIN — no family code to type. You'll also stay signed in for "
                "up to a week."
            )


# --- Logged-in router ------------------------------------------------------

def _build_navigation(user: dict):
    """Return an st.navigation object with pages for this user's role."""
    if user["role"] == "kid":
        pages = {
            "My KidBucks": [
                st.Page("views/wallet.py", title="My Wallet", icon="👛", default=True),
                st.Page("views/do_chores.py", title="Do Chores", icon="✅"),
                st.Page("views/redeem.py", title="Redeem", icon="🎁"),
                st.Page("views/account.py", title="My Account", icon="🔑"),
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
                st.Page("views/pocket_money.py", title="Pocket Money", icon="💵"),
                st.Page("views/family.py", title="Family", icon="👨‍👩‍👧‍👦"),
                st.Page("views/account.py", title="My Account", icon="🔑"),
            ]
        }
        if user["is_admin"]:
            pages["Admin"] = [
                st.Page("views/redemption_options.py", title="Redemption Options",
                        icon="💱"),
            ]
    return st.navigation(pages)


def _sidebar_account(user: dict) -> None:
    # Refresh the login/family cookies once per session so the "stay signed in"
    # window keeps rolling forward while the app is used.
    if not st.session_state.get("_cookies_fresh"):
        _refresh_cookies(user)
        st.session_state["_cookies_fresh"] = True
    with st.sidebar:
        st.markdown(f"### {user['emoji']} {user['name']}")
        role_label = "Admin parent" if user["is_admin"] else user["role"].capitalize()
        st.caption(role_label)
        st.caption(f"👨‍👩‍👧‍👦 {user['family_name']}  ·  `{user['family_code']}`")
        if st.button("Log out", width="stretch"):
            _cookie_del(AUTH_COOKIE)
            st.session_state.pop("_cookies_fresh", None)
            auth.logout()
            st.rerun()


# --- Main ------------------------------------------------------------------

user = auth.current_user()
if user is None:
    _try_cookie_login()          # auto-login from the saved cookie if present
    user = auth.current_user()
if user is None:
    _login_screen()
else:
    _sidebar_account(user)
    _build_navigation(user).run()
