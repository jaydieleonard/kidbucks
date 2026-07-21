"""Parent dashboard: overview of all linked kids + a detail view per kid."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import auth
import db

user = auth.require("parent")

st.title("📊 Parent Dashboard")

kids = db.list_kids_for_parent(user["id"])
outstanding = db.outstanding_approvals_for_parent(user["id"])

top1, top2, top3 = st.columns(3)
top1.metric("Your kids", len(kids))
top2.metric("Outstanding approvals", outstanding)
top3.metric(
    "Total in wallets",
    auth.fmt_bucks(sum(k["balance"] for k in kids)),
)

if outstanding:
    if st.button(f"🔔 Review {outstanding} pending approval(s)"):
        st.switch_page("views/approvals.py")

st.divider()

if not kids:
    st.info(
        "No kids linked to you yet. If you're the main parent, add or link kids "
        "on the **Family & Users** page. Otherwise ask the main parent to link you."
    )
    st.stop()

# --- Overview of every linked kid -----------------------------------------
st.subheader("Your kids")
overview = pd.DataFrame(
    [
        {
            "Kid": f"{k['emoji']} {k['name']}",
            "Balance": auth.fmt_bucks(k["balance"]),
        }
        for k in kids
    ]
)
st.dataframe(overview, hide_index=True, width="stretch")

st.divider()

# --- Selectable detail view ------------------------------------------------
st.subheader("Kid detail")
kid_by_label = {f"{k['emoji']} {k['name']}": k for k in kids}
label = st.selectbox("Select a kid", list(kid_by_label.keys()))
kid = kid_by_label[label]

summary = db.kid_summary(kid["id"])
d1, d2, d3, d4 = st.columns(4)
d1.metric("Balance", auth.fmt_bucks(summary["balance"]))
d2.metric("Earned", auth.fmt_bucks(summary["earned"]))
d3.metric("Spent", auth.fmt_bucks(summary["spent"]))
d4.metric("Pending", summary["pending"])

st.markdown("**Recent activity**")
txns = db.recent_transactions(kid["id"], limit=20)
if not txns:
    st.caption("No activity yet.")
else:
    df = pd.DataFrame(txns)
    df["When"] = pd.to_datetime(df["created_at"]).dt.strftime("%d %b %H:%M")
    df["Amount"] = df["amount"].apply(
        lambda a: f"+{a} {auth.BUCK}" if a >= 0 else f"{a} {auth.BUCK}"
    )
    df = df.rename(columns={"reason": "What", "type": "Type"})
    st.dataframe(
        df[["When", "What", "Type", "Amount"]],
        hide_index=True,
        width="stretch",
    )
