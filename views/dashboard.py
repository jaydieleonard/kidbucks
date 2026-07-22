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


# --- Quick actions: spontaneous reward / demerit --------------------------
def _quick_txn(linked_kids, *, kind: str) -> None:
    reward = kind == "reward"
    ttype = db.TXN_BONUS if reward else db.TXN_DEMERIT
    prefix = "Bonus" if reward else "Demerit"
    verb = "Give reward" if reward else "Apply demerit"
    with st.form(f"quick_{kind}"):
        kid_by = {f"{k['emoji']} {k['name']}": k for k in linked_kids}
        picked = st.selectbox("Which kid?", list(kid_by.keys()), key=f"qk_{kind}")
        desc = st.text_input(
            "What for?", key=f"qd_{kind}",
            placeholder="Helped a neighbour" if reward else "Was rude at dinner",
        )
        amount = st.number_input(
            f"KidBucks ({auth.BUCK})", min_value=1, step=1, value=5, key=f"qamt_{kind}"
        )
        if st.form_submit_button(verb, width="stretch"):
            if not desc.strip():
                st.error("Add a short description.")
            else:
                kid = kid_by[picked]
                amt = int(amount) if reward else -int(amount)
                db.add_transaction(kid["id"], amt, ttype, f"{prefix}: {desc.strip()}")
                emoji = "🎉" if reward else "⚠️"
                st.toast(
                    f"{emoji} {prefix} of {auth.fmt_bucks(int(amount))} for {kid['name']}"
                )
                st.rerun()


st.subheader("Quick actions")
st.caption("Reward good behaviour on the spot — or apply a one-off demerit.")
qa1, qa2, _ = st.columns([1, 1, 2])
with qa1.popover("⭐ Quick reward", width="stretch"):
    _quick_txn(kids, kind="reward")
with qa2.popover("⚠️ Quick demerit", width="stretch"):
    _quick_txn(kids, kind="demerit")

st.divider()

# --- Overview of every linked kid, with a quick PIN reset ------------------
st.subheader("Your kids")
st.caption("Forgot a PIN? Reset it here and tell them the new one.")
for k in kids:
    with st.container(border=True):
        c1, c2 = st.columns([3, 1])
        c1.markdown(f"**{k['emoji']} {k['name']}** — {auth.fmt_bucks(k['balance'])}")
        with c2.popover("🔑 Reset PIN", width="stretch"):
            with st.form(f"pinreset_{k['id']}"):
                new_pin = st.text_input(
                    f"New PIN for {k['name']}", type="password", key=f"np_{k['id']}"
                )
                if st.form_submit_button("Set PIN", width="stretch"):
                    if not new_pin.strip():
                        st.error("Enter a PIN.")
                    else:
                        secret_hash, salt = auth.hash_secret(new_pin)
                        db.reset_secret(k["id"], secret_hash, salt)
                        st.success(f"{k['name']}'s PIN updated — tell them the new one.")

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
