"""Kid landing page: wallet balance, a quick summary, and recent activity."""

from __future__ import annotations

import pandas as pd
import streamlit as st

import auth
import db

user = auth.require("kid")

summary = db.kid_summary(user["id"])

st.title(f"{user['emoji']} {user['name']}'s Wallet")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Balance", auth.fmt_bucks(summary["balance"]))
col2.metric("Earned (all time)", auth.fmt_bucks(summary["earned"]))
col3.metric("Spent (all time)", auth.fmt_bucks(summary["spent"]))
col4.metric("Waiting on parents", summary["pending"])

st.divider()

c1, c2 = st.columns(2)
with c1:
    if st.button("✅ Do Chores", width="stretch"):
        st.switch_page("views/do_chores.py")
with c2:
    if st.button("🎁 Redeem bucks", width="stretch"):
        st.switch_page("views/redeem.py")

st.divider()

# --- Pocket money tracker (per month) --------------------------------------
month = db.current_month()
pm = db.pocket_money_month_total(user["id"], month)
st.subheader("💵 Pocket money")
p1, p2 = st.columns(2)
p1.metric(
    f"Redeemed in {db.month_label(month)}", db.fmt_units(pm["units"], pm["unit_label"])
)
p2.metric("Not yet paid", db.fmt_units(pm["unpaid_units"], pm["unit_label"]))

history = [h for h in db.pocket_money_monthly_history(user["id"]) if h["month"] != month]
if history:
    with st.expander("Previous months"):
        for h in history:
            note = ("all paid" if h["paid_count"] == h["count"]
                    else f"{h['paid_count']}/{h['count']} paid")
            st.write(
                f"**{db.month_label(h['month'])}** — "
                f"{db.fmt_units(h['units'], h['unit_label'])}  ·  _{note}_"
            )

st.divider()

st.subheader("Recent activity")
txns = db.recent_transactions(user["id"], limit=25)
if not txns:
    st.info("No activity yet. Do a chore to start earning KidBucks!")
else:
    df = pd.DataFrame(txns)
    df["When"] = pd.to_datetime(df["created_at"]).dt.strftime("%d %b %H:%M")
    df["Amount"] = df["amount"].apply(
        lambda a: f"+{a} {auth.BUCK}" if a >= 0 else f"{a} {auth.BUCK}"
    )
    df = df.rename(columns={"reason": "What"})
    st.dataframe(
        df[["When", "What", "Amount"]],
        hide_index=True,
        width="stretch",
    )

# Pending chore submissions, so the kid can see what's awaiting approval.
pending = [s for s in db.kid_submissions(user["id"]) if s["status"] == "pending"]
if pending:
    st.subheader("Chores waiting for approval")
    for s in pending:
        st.write(f"⏳ **{s['chore_name']}** — {auth.fmt_bucks(s['value'])}")
