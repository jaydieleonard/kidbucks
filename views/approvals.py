"""Parent page: approve or reject pending chores and redemption requests.

Only requests from kids linked to this parent appear here.
"""

from __future__ import annotations

from datetime import datetime

import streamlit as st

import auth
import db

user = auth.require("parent")


def _fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %Y, %H:%M")
    except (ValueError, TypeError):
        return (iso or "")[:16].replace("T", " ")


st.title("🔔 Approvals")

chore_subs = db.pending_chore_submissions_for_parent(user["id"])
redemptions = db.pending_redemptions_for_parent(user["id"])

if not chore_subs and not redemptions:
    st.success("All caught up — nothing waiting for approval. 🎉")
    st.stop()

# --- Chore submissions -----------------------------------------------------
st.subheader(f"Chores to approve ({len(chore_subs)})")
if not chore_subs:
    st.caption("None right now.")
for s in chore_subs:
    with st.container(border=True):
        c1, c2, c3 = st.columns([3, 1, 1])
        with c1:
            st.markdown(
                f"{s['kid_emoji']} **{s['kid_name']}** did "
                f"**{s['chore_name']}** — {auth.fmt_bucks(s['value'])}"
            )
            st.caption(f"🕒 Requested {_fmt_dt(s['submitted_at'])}")
            if s["note"]:
                st.caption(f"Note: {s['note']}")
        with c2:
            if st.button("✅ Approve", key=f"ac_{s['id']}", width="stretch"):
                db.approve_chore_submission(s["id"], user["id"])
                st.rerun()
        with c3:
            if st.button("❌ Reject", key=f"rc_{s['id']}", width="stretch"):
                db.reject_chore_submission(s["id"], user["id"])
                st.rerun()

st.divider()

# --- Redemption requests ---------------------------------------------------
st.subheader(f"Redemptions to approve ({len(redemptions)})")
if not redemptions:
    st.caption("None right now.")
for r in redemptions:
    with st.container(border=True):
        c1, c2, c3 = st.columns([3, 1, 1])
        with c1:
            st.markdown(
                f"{r['kid_emoji']} **{r['kid_name']}** wants "
                f"**{db.fmt_units(r['units'], r['unit_label'])}** of {r['option_name']} "
                f"for {auth.fmt_bucks(r['bucks_spent'])}"
            )
            st.caption(f"🕒 Requested {_fmt_dt(r['requested_at'])}")
            affordable = r["bucks_spent"] <= r["kid_balance"]
            note = f"Current balance: {auth.fmt_bucks(r['kid_balance'])}"
            if affordable:
                st.caption(note)
            else:
                st.caption(f"⚠️ {note} — not enough bucks anymore.")
        with c2:
            if st.button("✅ Approve", key=f"ar_{r['id']}", width="stretch"):
                ok, msg = db.approve_redemption(r["id"], user["id"])
                (st.success if ok else st.error)(msg)
                if ok:
                    st.rerun()
        with c3:
            if st.button("❌ Reject", key=f"rr_{r['id']}", width="stretch"):
                db.reject_redemption(r["id"], user["id"])
                st.rerun()
