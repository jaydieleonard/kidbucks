"""Kid page: tick the chores you've done and send them for parent approval."""

from __future__ import annotations

import streamlit as st

import auth
import db

user = auth.require("kid")

st.title("✅ Do Chores")
st.caption("Tick everything you've done, then send it to your parents to approve.")

available = db.available_chores_for_kid(user["id"])

if not available:
    st.info(
        "No chores available right now. You may have already submitted them all "
        "(check your wallet), or a parent still needs to add some."
    )
else:
    with st.form("submit_chores"):
        st.write("**Which chores did you do?**")
        picked: list[int] = []
        for chore in available:
            tag = db.RECURRENCE_LABELS.get(chore["recurrence"], chore["recurrence"])
            shared_tag = "  · 👪 family task — first come!" if chore["shared"] else ""
            label = (
                f"{chore['name']} — {auth.fmt_bucks(chore['value'])}  "
                f"({tag}){shared_tag}"
            )
            if st.checkbox(label, key=f"chore_{chore['id']}"):
                picked.append(chore["id"])
            if chore["description"]:
                st.caption(chore["description"])

        if st.form_submit_button("Send for approval", width="stretch"):
            if not picked:
                st.warning("Pick at least one chore first.")
            else:
                count = sum(
                    1 for cid in picked if db.submit_chore(user["id"], cid) is not None
                )
                st.success(
                    f"Sent {count} chore(s) to your parents for approval! 🎉"
                )
                st.rerun()

st.divider()

st.subheader("Your recent chores")
subs = db.kid_submissions(user["id"], limit=20)
if not subs:
    st.caption("Nothing yet.")
else:
    status_icon = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
    for s in subs:
        icon = status_icon.get(s["status"], "•")
        st.write(
            f"{icon} **{s['chore_name']}** — {auth.fmt_bucks(s['value'])} "
            f"· _{s['status']}_"
        )
