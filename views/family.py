"""Family page (all parents): invite others, assign kids to yourself, and — for
the admin — manage everyone in the household.
"""

from __future__ import annotations

import streamlit as st

import auth
import db

user = auth.require("parent")
fam_id = user["family_id"]

EMOJI_CHOICES = auth.EMOJI_CHOICES

st.title("👨‍👩‍👧‍👦 " + user["family_name"])

# Show the newly created code once, right after creating a family.
if st.session_state.pop("just_created_code", None):
    st.success("Family created! Share the code below so others can join.")

tabs = ["Invite", "Assign kids"]
if user["is_admin"]:
    tabs.append("Manage users")
rendered = st.tabs(tabs)


# --- Invite ----------------------------------------------------------------
with rendered[0]:
    code = user["family_code"]
    st.subheader("Your family code")
    st.code(code, language=None)
    st.caption("Anyone with this code can join your family. Tap the copy icon above.")

    # Best-effort shareable join link using the request Host header.
    join_url = None
    try:
        host = st.context.headers.get("Host")
        if host:
            join_url = f"http://{host}/?family={code}"
    except Exception:
        join_url = None

    message = auth.invite_message(user["family_name"], code, join_url)
    st.markdown("**Invite message** (copy or use a button below):")
    st.code(message, language=None)

    links = auth.invite_links(user["family_name"], code, join_url)
    c1, c2, c3 = st.columns(3)
    c1.link_button("💬 Share via SMS", links["sms"], width="stretch")
    c2.link_button("🟢 WhatsApp", links["whatsapp"], width="stretch")
    c3.link_button("✉️ Email", links["email"], width="stretch")
    if join_url:
        st.caption(f"Join link: {join_url}")


# --- Assign kids to me -----------------------------------------------------
with rendered[1]:
    st.subheader("Kids in your family")
    st.caption("Assign the kids you're responsible for — you'll approve their chores.")
    kids = db.list_all_kids(fam_id)
    parents = db.list_parents(fam_id)
    id_to_parent = {p["id"]: p for p in parents}

    if not kids:
        st.info("No kids yet. Share your code so they can join.")
    for k in kids:
        linked = db.get_kid_parent_ids(k["id"])
        assigned_to_me = user["id"] in linked
        linked_names = [id_to_parent[i]["name"] for i in linked if i in id_to_parent]
        with st.container(border=True):
            col1, col2 = st.columns([3, 1])
            with col1:
                st.markdown(f"**{k['emoji']} {k['name']}** — {auth.fmt_bucks(k['balance'])}")
                if linked_names:
                    st.caption("Parents: " + ", ".join(linked_names))
                else:
                    st.caption("⚠️ Not assigned to any parent yet.")
            with col2:
                if assigned_to_me:
                    if st.button("Unassign me", key=f"un_{k['id']}", width="stretch"):
                        db.unlink_parent_kid(user["id"], k["id"])
                        st.rerun()
                else:
                    if st.button("Assign to me", key=f"as_{k['id']}", width="stretch"):
                        db.link_parent_kid(user["id"], k["id"])
                        st.rerun()


# --- Admin: manage users ---------------------------------------------------
if user["is_admin"]:
    with rendered[2]:
        st.info(
            "🔒 For everyone's safety, passwords & PINs are stored **encrypted** and "
            "can't be shown. You can see every family member below and **reset** "
            "anyone's password/PIN to a new value you choose."
        )
        parents = db.list_parents(fam_id)
        parent_by_name = {p["name"]: p["id"] for p in parents}
        id_to_parent_name = {p["id"]: p["name"] for p in parents}
        admin_count = sum(1 for p in parents if p["is_admin"])

        st.subheader(f"Parents ({len(parents)})")
        for p in parents:
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 1, 1])
                badge = " 👑 admin" if p["is_admin"] else ""
                c1.markdown(f"**{p['emoji']} {p['name']}**{badge}  \n`{p['username']}`")
                with c2:
                    if p["is_admin"]:
                        disabled = admin_count <= 1
                        if st.button("Revoke admin", key=f"rv_{p['id']}",
                                     width="stretch", disabled=disabled,
                                     help="At least one admin must remain."
                                     if disabled else None):
                            db.set_admin(p["id"], False)
                            st.rerun()
                    else:
                        if st.button("Make admin", key=f"mk_{p['id']}", width="stretch"):
                            db.set_admin(p["id"], True)
                            st.rerun()
                with c3:
                    can_delete = p["id"] != user["id"] and not (
                        p["is_admin"] and admin_count <= 1
                    )
                    if st.button("🗑️ Delete", key=f"delp_{p['id']}",
                                 width="stretch", disabled=not can_delete):
                        db.delete_user(p["id"])
                        st.rerun()
                with st.expander("Reset password"):
                    with st.form(f"pwreset_{p['id']}"):
                        newpw = st.text_input(
                            "New password", type="password", key=f"pw_{p['id']}"
                        )
                        if st.form_submit_button("Set new password", width="stretch"):
                            if not newpw:
                                st.error("Enter a new password.")
                            else:
                                h, s = auth.hash_secret(newpw)
                                db.reset_secret(p["id"], h, s)
                                st.success(f"Password reset for {p['name']}.")

        st.divider()
        st.subheader("Kids")
        kids = db.list_all_kids(fam_id)
        for k in kids:
            with st.container(border=True):
                st.markdown(f"**{k['emoji']} {k['name']}** — {auth.fmt_bucks(k['balance'])}")
                linked = db.get_kid_parent_ids(k["id"])
                default = [id_to_parent_name[i] for i in linked if i in id_to_parent_name]
                with st.form(f"kid_admin_{k['id']}"):
                    chosen = st.multiselect(
                        "Linked parents", list(parent_by_name.keys()), default=default,
                        key=f"ml_{k['id']}",
                    )
                    new_pin = st.text_input(
                        "Reset PIN (blank = keep)", type="password", key=f"pin_{k['id']}"
                    )
                    b1, b2 = st.columns(2)
                    with b1:
                        if st.form_submit_button("Save", width="stretch"):
                            db.set_kid_parents(k["id"], [parent_by_name[n] for n in chosen])
                            if new_pin:
                                h, s = auth.hash_secret(new_pin)
                                db.reset_secret(k["id"], h, s)
                            st.success("Saved.")
                            st.rerun()
                    with b2:
                        if st.form_submit_button("🗑️ Delete kid", width="stretch"):
                            db.delete_user(k["id"])
                            st.rerun()

        st.divider()
        with st.expander("➕ Add someone manually"):
            kind = st.radio("Add a…", ["Kid", "Parent"], horizontal=True, key="add_kind")
            if kind == "Kid":
                with st.form("add_kid_admin"):
                    name = st.text_input("Kid's name")
                    emoji = st.selectbox("Avatar", EMOJI_CHOICES, key="ke")
                    pin = st.text_input("PIN", type="password")
                    chosen = st.multiselect("Linked parents", list(parent_by_name.keys()))
                    if st.form_submit_button("Create kid", width="stretch"):
                        if not name.strip() or not pin:
                            st.error("Name and PIN are required.")
                        elif db.kid_name_exists(fam_id, name.strip()):
                            st.error("A kid with that name already exists.")
                        else:
                            h, s = auth.hash_secret(pin)
                            uid = db.create_user(fam_id, name.strip(), "kid", h, s,
                                                 emoji=emoji)
                            db.set_kid_parents(uid, [parent_by_name[n] for n in chosen])
                            st.success(f"Created {name.strip()}.")
                            st.rerun()
            else:
                with st.form("add_parent_admin"):
                    name = st.text_input("Display name")
                    username = st.text_input("Username")
                    emoji = st.selectbox("Avatar", EMOJI_CHOICES, key="pe")
                    password = st.text_input("Password", type="password")
                    make_admin = st.checkbox("Grant admin rights")
                    if st.form_submit_button("Create parent", width="stretch"):
                        if not name.strip() or not username.strip() or not password:
                            st.error("Name, username and password are required.")
                        elif db.username_exists(fam_id, username.strip()):
                            st.error("That username is already taken in this family.")
                        else:
                            h, s = auth.hash_secret(password)
                            db.create_user(fam_id, name.strip(), "parent", h, s,
                                           username=username.strip(),
                                           is_admin=make_admin, emoji=emoji)
                            st.success(f"Created {name.strip()}.")
                            st.rerun()
