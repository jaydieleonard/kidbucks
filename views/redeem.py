"""Kid page: convert KidBucks into pocket money, screen time, etc.

The kid chooses an option and how much they want; the buck cost is shown, and
submitting sends a request to a parent for approval before any bucks leave the
wallet.
"""

from __future__ import annotations

import math

import streamlit as st

import auth
import db

user = auth.require("kid")

balance = db.get_balance(user["id"])

st.title("🎁 Redeem KidBucks")
st.metric("Your balance", auth.fmt_bucks(balance))

options = db.list_redemption_options(user["family_id"], active_only=True)
if not options:
    st.info("No ways to redeem yet. Ask the main parent to set some up.")
    st.stop()

st.caption("Choose what you'd like, then send the request to a parent to approve.")

opt_by_label = {f"{o['name']} ({o['unit']})": o for o in options}
choice = st.selectbox("What would you like?", list(opt_by_label.keys()))
opt = opt_by_label[choice]

# Each kid may have their own rate for a per-child option (e.g. pocket money).
rate = db.effective_rate(opt["id"], user["id"])

# The most of this unit the kid can get without going negative. Cost rounds up
# and balances are whole bucks, so floor(balance / rate) is the exact maximum.
max_units = math.floor(balance / rate) if rate and rate > 0 and balance > 0 else 0

st.write(f"Your rate: **{rate:g} {auth.BUCK}** per 1 {opt['unit']}")

if max_units >= 1:
    st.success(
        f"💰 With your {auth.fmt_bucks(balance)} you can redeem up to "
        f"**{db.fmt_units(max_units, opt['unit'])}** of {opt['name']}."
    )
    with st.form("redeem"):
        units = st.number_input(
            f"How much {opt['name']}? (max {db.fmt_units(max_units, opt['unit'])})",
            min_value=1.0, max_value=float(max_units), step=1.0, value=1.0,
        )
        cost = db.redemption_cost(rate, units)
        st.write(
            f"Cost: **{auth.fmt_bucks(cost)}** for {db.fmt_units(units, opt['unit'])} · "
            f"you'd have {auth.fmt_bucks(balance - cost)} left"
        )
        if st.form_submit_button("Request redemption", width="stretch"):
            req = db.request_redemption(user["id"], opt["id"], units)
            if req is None:
                st.error("Couldn't submit — you don't have enough KidBucks for that.")
            else:
                st.success("Request sent! A parent will approve it soon. 🎉")
                st.rerun()
else:
    st.warning(
        f"You don't have enough KidBucks to redeem {opt['name']} yet"
        + (" (your balance is negative)." if balance < 0 else " — do some chores to earn more!")
    )

st.divider()

st.subheader("Your redemption history")
reqs = db.kid_redemptions(user["id"], limit=20)
if not reqs:
    st.caption("Nothing yet.")
else:
    status_icon = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
    for r in reqs:
        icon = status_icon.get(r["status"], "•")
        st.write(
            f"{icon} {db.fmt_units(r['units'], r['unit_label'])} of "
            f"**{r['option_name']}** — {auth.fmt_bucks(r['bucks_spent'])} "
            f"· _{r['status']}_"
        )
