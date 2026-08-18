"""Parent setup page: create, edit and archive chores, their value, recurrence
and whether they're a shared (first-come) family task."""

from __future__ import annotations

import streamlit as st

import auth
import db

_fmt_dt = getattr(db, "fmt_dt", lambda iso, *_a: (iso or "")[:16].replace("T", " "))

user = auth.require("parent")
fam_id = user["family_id"]

RECS = db.RECURRENCE_OPTIONS
def _rec_label(r: str) -> str:
    return db.RECURRENCE_LABELS.get(r, r)

st.title("🧹 Chores")
st.caption("List the chores kids can do, how much they're worth, and how often.")

# --- Add a new chore -------------------------------------------------------
with st.expander("➕ Add a new chore", expanded=False):
    with st.form("add_chore"):
        name = st.text_input("Chore name")
        col1, col2 = st.columns(2)
        with col1:
            value = st.number_input(f"Worth ({auth.BUCK})", min_value=1, step=1, value=10)
        with col2:
            recurrence = st.selectbox(
                "How often?", RECS, format_func=_rec_label,
                help="One-time chores disappear once approved. Recurring chores can "
                     "be earned again each day/week/month.",
            )
        shared = st.checkbox(
            "👪 Family task — only one kid can do it each period (first come)"
        )
        description = st.text_area("Description (optional)")
        if st.form_submit_button("Add chore", width="stretch"):
            if not name.strip():
                st.error("Give the chore a name.")
            else:
                db.create_chore(
                    fam_id, name.strip(), int(value), recurrence, shared,
                    description.strip(), created_by=user["id"],
                )
                st.success(f"Added “{name.strip()}”.")
                st.rerun()

st.divider()

# --- Existing chores -------------------------------------------------------
chores = db.list_chores(fam_id)
active = [c for c in chores if c["active"]]
archived = [c for c in chores if not c["active"]]

st.subheader(f"Active chores ({len(active)})")
if not active:
    st.caption("No active chores yet. Add one above.")

for c in active:
    with st.container(border=True):
        with st.form(f"edit_chore_{c['id']}"):
            col1, col2 = st.columns([3, 2])
            with col1:
                new_name = st.text_input("Name", value=c["name"], key=f"n{c['id']}")
                new_desc = st.text_input(
                    "Description", value=c["description"], key=f"d{c['id']}"
                )
            with col2:
                new_value = st.number_input(
                    f"Worth ({auth.BUCK})", min_value=1, step=1,
                    value=c["value"], key=f"v{c['id']}",
                )
                new_rec = st.selectbox(
                    "How often?", RECS, index=RECS.index(c["recurrence"]),
                    format_func=_rec_label, key=f"r{c['id']}",
                )
            new_shared = st.checkbox(
                "👪 Family task (one kid per period)", value=bool(c["shared"]),
                key=f"sh{c['id']}",
            )
            b1, b2 = st.columns(2)
            with b1:
                if st.form_submit_button("Save", width="stretch"):
                    db.update_chore(
                        c["id"], new_name.strip(), int(new_value), new_rec,
                        new_shared, new_desc.strip(), actor_id=user["id"],
                    )
                    st.success("Saved.")
                    st.rerun()
            with b2:
                if st.form_submit_button("Archive", width="stretch"):
                    db.set_chore_active(c["id"], False, actor_id=user["id"])
                    st.rerun()

if archived:
    with st.expander(f"Archived chores ({len(archived)})"):
        for c in archived:
            col1, col2 = st.columns([4, 1])
            col1.write(f"~~{c['name']}~~ — {auth.fmt_bucks(c['value'])}")
            if col2.button("Restore", key=f"restore_{c['id']}"):
                db.set_chore_active(c["id"], True, actor_id=user["id"])
                st.rerun()

st.divider()

# --- Change log ------------------------------------------------------------
with st.expander("📜 Change log — who changed what"):
    # hasattr guard: tolerate a deploy where this file is ahead of db.py.
    entries = db.chore_audit_log(fam_id, limit=50) if hasattr(db, "chore_audit_log") else []
    if not entries:
        st.caption("No changes logged yet.")
    else:
        icons = {"created": "➕", "edited": "✏️", "archived": "🗄️", "restored": "♻️"}
        for e in entries:
            when = _fmt_dt(e["created_at"], "%Y-%m-%d %H:%M")
            who = e["actor_name"] or "Someone"
            icon = icons.get(e["action"], "•")
            line = f"{icon} **{who}** {e['action']} “{e['chore_name']}”"
            if e["detail"]:
                line += f" — {e['detail']}"
            st.write(f"{line}  \n_{when}_")
