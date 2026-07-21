"""Admin-only page: manage redemption options and their conversion rates.

Adding a row here surfaces a brand-new way to spend KidBucks on the kids' Redeem
page — no code change needed. Only the main parent (admin) can change rates. An
option can allow **per-child rates** (e.g. Pocket Money), letting the admin set a
different rate for individual kids on top of the family default.
"""

from __future__ import annotations

import streamlit as st

import auth
import db

user = auth.require("parent", admin=True)
fam_id = user["family_id"]

st.title("💱 Redemption Options")
st.caption(
    "Decide what KidBucks can be exchanged for and set the conversion rate. "
    "Rate = how many KidBucks are needed for one unit."
)

kids = db.list_all_kids(fam_id)

# --- Add a new option ------------------------------------------------------
with st.expander("➕ Add a redemption option", expanded=False):
    with st.form("add_option"):
        name = st.text_input("Name", placeholder="e.g. Pocket Money, Screen Time")
        unit = st.text_input("Unit", placeholder="e.g. R, $, minutes")
        rate = st.number_input(
            f"Default rate ({auth.BUCK} per unit)", min_value=0.1, step=0.5, value=10.0
        )
        per_child = st.checkbox("Allow a different rate per child")
        if st.form_submit_button("Add option", width="stretch"):
            if not name.strip() or not unit.strip():
                st.error("Name and unit are required.")
            else:
                db.create_redemption_option(
                    fam_id, name.strip(), unit.strip(), float(rate), per_child
                )
                st.success(f"Added “{name.strip()}”.")
                st.rerun()

st.divider()

# --- Existing options ------------------------------------------------------
options = db.list_redemption_options(fam_id)
if not options:
    st.info("No redemption options yet. Add one above.")

for o in options:
    with st.container(border=True):
        with st.form(f"opt_{o['id']}"):
            col1, col2, col3 = st.columns([2, 1, 1])
            with col1:
                new_name = st.text_input("Name", value=o["name"], key=f"on{o['id']}")
            with col2:
                new_unit = st.text_input("Unit", value=o["unit"], key=f"ou{o['id']}")
            with col3:
                new_rate = st.number_input(
                    f"Default {auth.BUCK}/unit", min_value=0.1, step=0.5,
                    value=float(o["bucks_per_unit"]), key=f"or{o['id']}",
                )
            new_per_child = st.checkbox(
                "Allow a different rate per child", value=bool(o["per_child"]),
                key=f"opc{o['id']}",
            )
            b1, b2 = st.columns(2)
            with b1:
                if st.form_submit_button("Save", width="stretch"):
                    db.update_redemption_option(
                        o["id"], new_name.strip(), new_unit.strip(),
                        float(new_rate), new_per_child,
                    )
                    st.success("Saved.")
                    st.rerun()
            with b2:
                toggle = "Deactivate" if o["active"] else "Activate"
                if st.form_submit_button(toggle, width="stretch"):
                    db.set_redemption_option_active(o["id"], not o["active"])
                    st.rerun()

        # Per-child rate editor (only when this option allows it).
        if o["per_child"] and kids:
            with st.expander(f"Per-child rates for {o['name']}"):
                st.caption(
                    f"Leave a child at the default ({o['bucks_per_unit']:g} "
                    f"{auth.BUCK}/{o['unit']}) or give them their own rate."
                )
                with st.form(f"rates_{o['id']}"):
                    entered = {}
                    for k in kids:
                        entered[k["id"]] = st.number_input(
                            f"{k['emoji']} {k['name']} ({auth.BUCK}/{o['unit']})",
                            min_value=0.1, step=0.5,
                            value=float(db.effective_rate(o["id"], k["id"])),
                            key=f"rate_{o['id']}_{k['id']}",
                        )
                    if st.form_submit_button("Save per-child rates", width="stretch"):
                        for kid_id, val in entered.items():
                            if abs(val - o["bucks_per_unit"]) < 1e-9:
                                db.clear_kid_rate(o["id"], kid_id)  # follow default
                            else:
                                db.set_kid_rate(o["id"], kid_id, float(val))
                        st.success("Per-child rates saved.")
                        st.rerun()
