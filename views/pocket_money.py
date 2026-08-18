"""Parent page: track pocket money redeemed by linked kids.

Shows this month's real-money redemptions per kid with a Paid toggle (tick it
once you've handed over the cash), plus a per-month history for earlier months.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import auth
import db

_fmt_dt = getattr(db, "fmt_dt", lambda iso, *_a: (iso or "")[:16].replace("T", " "))

user = auth.require("parent")
month = db.current_month()

st.title("💵 Pocket Money")
st.caption(
    "See what each child has redeemed for real money and mark it paid once you've "
    f"handed it over. Current month: **{db.month_label(month)}**."
)

kids = db.list_kids_for_parent(user["id"])
if not kids:
    st.info("No kids linked to you yet. Assign kids to yourself on the Family page.")
    st.stop()

# --- This month at a glance, across all your kids --------------------------
st.subheader(f"This month — {db.month_label(month)}")
for k in kids:
    tot = db.pocket_money_month_total(k["id"], month)
    c1, c2, c3 = st.columns([3, 2, 2])
    c1.markdown(f"**{k['emoji']} {k['name']}**")
    c2.markdown(f"Redeemed: {db.fmt_units(tot['units'], tot['unit_label'])}")
    if tot["unpaid_count"]:
        c3.markdown(f"⚠️ To pay: **{db.fmt_units(tot['unpaid_units'], tot['unit_label'])}**")
    else:
        c3.markdown("✅ all paid")

st.divider()

# --- Per-kid detail --------------------------------------------------------
kid_by = {f"{k['emoji']} {k['name']}": k for k in kids}
picked = st.selectbox("Choose a kid", list(kid_by.keys()))
kid = kid_by[picked]

st.subheader(f"{db.month_label(month)} · {kid['name']}")
this_month = db.pocket_money_redemptions(kid["id"], month)
if not this_month:
    st.caption("No pocket money redeemed this month yet.")
else:
    for r in this_month:
        with st.container(border=True):
            c1, c2, c3 = st.columns([2, 2, 2])
            when = _fmt_dt(r["reviewed_at"] or r["requested_at"] or "", "%d %b")
            c1.markdown(
                f"**{db.fmt_units(r['units'], r['unit_label'])}**  \n_{when}_"
            )
            c2.caption(f"Cost: {auth.fmt_bucks(r['bucks_spent'])}")
            with c3:
                paid = st.checkbox("Paid", value=bool(r["paid"]), key=f"paid_{r['id']}")
                if paid != bool(r["paid"]):
                    db.mark_redemption_paid(r["id"], paid)
                    st.rerun()
    tot = db.pocket_money_month_total(kid["id"], month)
    st.markdown(
        f"**Total this month:** {db.fmt_units(tot['units'], tot['unit_label'])}"
        f"  ·  **Still to pay:** {db.fmt_units(tot['unpaid_units'], tot['unit_label'])}"
    )

st.divider()

# --- Previous months -------------------------------------------------------
st.subheader("Previous months")
history = [h for h in db.pocket_money_monthly_history(kid["id"]) if h["month"] != month]
if not history:
    st.caption("No history yet.")
else:
    df = pd.DataFrame(
        [
            {
                "Month": db.month_label(h["month"]),
                "Pocket money": db.fmt_units(h["units"], h["unit_label"]),
                "Redemptions": h["count"],
                "Paid": f"{h['paid_count']}/{h['count']}",
            }
            for h in history
        ]
    )
    st.dataframe(df, hide_index=True, width="stretch")
