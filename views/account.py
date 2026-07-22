"""Account page (everyone): change your own password / PIN.

You need your current secret to change it. If you've forgotten it, ask a parent
(admin) to reset it for you from the Family page.
"""

from __future__ import annotations

import streamlit as st

import auth
import db

user = auth.require()  # any logged-in user

is_kid = user["role"] == "kid"
word = "PIN" if is_kid else "password"

st.title("🔑 My Account")
st.markdown(f"### {user['emoji']} {user['name']}")
st.caption(f"{'Kid' if is_kid else 'Parent'} · 👨‍👩‍👧‍👦 {user['family_name']}")

st.divider()
st.subheader(f"Change my {word}")

with st.form("change_secret"):
    current = st.text_input(f"Current {word}", type="password")
    new = st.text_input(f"New {word}", type="password")
    confirm = st.text_input(f"Confirm new {word}", type="password")
    if st.form_submit_button("Update", width="stretch"):
        row = db.get_user(user["id"])
        if not row or not auth.verify_secret(current, row["salt"], row["secret_hash"]):
            st.error(f"Your current {word} is incorrect.")
        elif not new:
            st.error(f"Please enter a new {word}.")
        elif new != confirm:
            st.error(f"The new {word}s don't match.")
        elif new == current:
            st.warning(f"That's already your {word} — pick a new one.")
        else:
            secret_hash, salt = auth.hash_secret(new)
            db.reset_secret(user["id"], secret_hash, salt)
            st.success(f"Your {word} has been updated. ✅")

st.caption(f"Forgotten your {word}? A parent can reset it for you from the Family page.")
