"""Account page (everyone): change your icon. Parents can also change their
password. Kids' PINs are reset by the main parent (admin) only."""

from __future__ import annotations

import streamlit as st

import auth
import db

user = auth.require()  # any logged-in user
is_kid = user["role"] == "kid"

EMOJI_CHOICES = auth.EMOJI_CHOICES

st.title("🔑 My Account")
st.markdown(f"### {user['emoji']} {user['name']}")
role_label = "Admin parent" if user["is_admin"] else ("Kid" if is_kid else "Parent")
st.caption(f"{role_label} · 👨‍👩‍👧‍👦 {user['family_name']}")

st.divider()

# --- Change icon (everyone) ------------------------------------------------
st.subheader("Change my icon")
options = (
    EMOJI_CHOICES if user["emoji"] in EMOJI_CHOICES
    else [user["emoji"]] + EMOJI_CHOICES
)
with st.form("change_icon"):
    emoji = st.selectbox("Pick your avatar", options, index=options.index(user["emoji"]))
    if st.form_submit_button("Save icon", width="stretch"):
        db.update_user_profile(user["id"], user["name"], emoji)
        auth.login(db.get_user(user["id"]))  # refresh the session so it shows at once
        st.success("Icon updated! 🎉")
        st.rerun()

st.divider()

# --- Change password (parents only) ---------------------------------------
if is_kid:
    st.subheader("My PIN")
    st.info("🔒 To change your PIN, ask the main parent — they can reset it for you.")
else:
    st.subheader("Change my password")
    with st.form("change_password"):
        current = st.text_input("Current password", type="password")
        new = st.text_input("New password", type="password")
        confirm = st.text_input("Confirm new password", type="password")
        if st.form_submit_button("Update password", width="stretch"):
            row = db.get_user(user["id"])
            if not row or not auth.verify_secret(current, row["salt"], row["secret_hash"]):
                st.error("Your current password is incorrect.")
            elif not new:
                st.error("Please enter a new password.")
            elif new != confirm:
                st.error("The new passwords don't match.")
            elif new == current:
                st.warning("That's already your password — pick a new one.")
            else:
                secret_hash, salt = auth.hash_secret(new)
                db.reset_secret(user["id"], secret_hash, salt)
                st.success("Your password has been updated. ✅")

st.divider()

# --- Add to iPhone home screen ---------------------------------------------
with st.expander("📲 Add KidBucks to your iPhone home screen"):
    st.markdown(
        "1. Open KidBucks in **Safari**.\n"
        "2. Tap the **Share** button (a square with an arrow pointing up).\n"
        "3. Scroll down and tap **Add to Home Screen** → **Add**.\n\n"
        "KidBucks then opens from your home screen like an app. The icon remembers "
        "your family, so you only ever tap your name and PIN — and it keeps you "
        "signed in for up to a week."
    )
