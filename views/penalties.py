"""Parent page: create penalty templates and apply them to deduct KidBucks."""

from __future__ import annotations

import streamlit as st

import auth
import db

user = auth.require("parent")
fam_id = user["family_id"]

st.title("⚠️ Penalties")
st.caption("Set up penalties for misbehaviour, then apply one to deduct KidBucks.")

# --- Apply a penalty -------------------------------------------------------
kids = db.list_kids_for_parent(user["id"])
penalties = db.list_penalties(fam_id, active_only=True)

st.subheader("Apply a penalty")
if not kids:
    st.info("No kids linked to you yet.")
elif not penalties:
    st.info("No penalties defined yet. Create one below first.")
else:
    with st.form("apply_penalty"):
        kid_by_label = {f"{k['emoji']} {k['name']}": k for k in kids}
        kid_label = st.selectbox("Kid", list(kid_by_label.keys()))
        pen_by_label = {
            f"{p['name']} (−{p['value']} {auth.BUCK})": p for p in penalties
        }
        pen_label = st.selectbox("Penalty", list(pen_by_label.keys()))
        note = st.text_input("Note (optional)")
        if st.form_submit_button("Apply penalty", width="stretch"):
            kid = kid_by_label[kid_label]
            pen = pen_by_label[pen_label]
            db.apply_penalty(kid["id"], pen["id"], user["id"], note.strip())
            st.success(
                f"Applied “{pen['name']}” to {kid['name']} "
                f"(−{auth.fmt_bucks(pen['value'])})."
            )
            st.rerun()

st.divider()

# --- Create a penalty template --------------------------------------------
with st.expander("➕ Create a new penalty", expanded=not penalties):
    with st.form("add_penalty"):
        name = st.text_input("Penalty name")
        value = st.number_input(
            f"Deduct ({auth.BUCK})", min_value=1, step=1, value=10
        )
        description = st.text_area("Description (optional)")
        if st.form_submit_button("Create penalty", width="stretch"):
            if not name.strip():
                st.error("Give the penalty a name.")
            else:
                db.create_penalty(
                    fam_id, name.strip(), int(value), description.strip(),
                    created_by=user["id"],
                )
                st.success(f"Created “{name.strip()}”.")
                st.rerun()

# --- Existing penalties ----------------------------------------------------
st.subheader("Penalty list")
all_penalties = db.list_penalties(fam_id)
if not all_penalties:
    st.caption("None yet.")
for p in all_penalties:
    with st.container(border=True):
        col1, col2, col3 = st.columns([3, 1, 1])
        col1.markdown(f"**{p['name']}** — −{auth.fmt_bucks(p['value'])}")
        if p["description"]:
            col1.caption(p["description"])
        if not p["active"]:
            col1.caption("_(inactive)_")
        if p["active"]:
            if col2.button("Deactivate", key=f"deact_{p['id']}",
                           width="stretch"):
                db.set_penalty_active(p["id"], False)
                st.rerun()
        else:
            if col2.button("Activate", key=f"act_{p['id']}",
                           width="stretch"):
                db.set_penalty_active(p["id"], True)
                st.rerun()
