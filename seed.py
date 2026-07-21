"""Seed a demo KidBucks household so the app has data to explore.

Run with:  python seed.py
Safe to run repeatedly — it clears all existing rows first.
"""

from __future__ import annotations

import auth
import db

# A fixed, friendly code for the demo family so it's easy to log in with.
FAMILY_NAME = "The Leonards"
FAMILY_CODE = "FAMILY-DEMO"

PARENTS = [
    # name, username, password, emoji, is_admin
    ("Mom", "mom", "mom123", "🌟", True),
    ("Dad", "dad", "dad123", "😎", False),
]

KIDS = [
    # name, pin, emoji, linked parent usernames
    ("Ava", "1111", "🦄", ["mom", "dad"]),
    ("Ben", "2222", "🚀", ["mom"]),
]

CHORES = [
    # name, value, recurrence, shared, description
    ("Make your bed", 5, "daily", False, "Every morning."),
    ("Wash the dishes", 10, "daily", False, "After dinner."),
    ("Feed the dog", 5, "daily", False, ""),
    ("Tidy your room", 15, "weekly", False, ""),
    ("Take out the bins", 20, "weekly", True, "Family task — whoever does it first!"),
    ("Wash the car", 50, "once", False, "A big one-time job."),
]

PENALTIES = [
    # name, value, description
    ("Back-talk", 10, "Being rude to a grown-up."),
    ("Missed homework", 15, "Homework not done on time."),
    ("Left a mess", 5, ""),
]

REDEMPTIONS = [
    # name, unit, bucks_per_unit, per_child (different rate allowed per kid)
    ("Pocket Money", "R", 10.0, True),    # 10 KidBucks = R1 (per-child allowed)
    ("Screen Time", "min", 2.0, False),   # 2 KidBucks = 1 minute (same for all)
]

# Deleted children-first so foreign keys stay satisfied.
_TABLES = [
    "transactions", "chore_submissions", "redemption_requests",
    "penalty_applications", "parent_kid", "chores", "penalties",
    "redemption_options", "users", "families",
]


def main() -> None:
    db.init_db()

    with db.get_connection() as conn:
        for table in _TABLES:
            conn.execute(f"DELETE FROM {table}")
        fam_id = conn.insert(
            "INSERT INTO families (name, code, created_at) VALUES (?, ?, ?)",
            (FAMILY_NAME, FAMILY_CODE, db._now()),
        )

    # Parents
    parent_ids: dict[str, int] = {}
    for name, username, password, emoji, is_admin in PARENTS:
        secret_hash, salt = auth.hash_secret(password)
        parent_ids[username] = db.create_user(
            fam_id, name, "parent", secret_hash, salt,
            username=username, is_admin=is_admin, emoji=emoji,
        )

    # Kids + links
    kid_ids: dict[str, int] = {}
    for name, pin, emoji, parents in KIDS:
        secret_hash, salt = auth.hash_secret(pin)
        kid_id = db.create_user(fam_id, name, "kid", secret_hash, salt, emoji=emoji)
        kid_ids[name] = kid_id
        db.set_kid_parents(kid_id, [parent_ids[u] for u in parents])

    # Chores
    chore_ids: dict[str, int] = {}
    for name, value, recurrence, shared, desc in CHORES:
        chore_ids[name] = db.create_chore(
            fam_id, name, value, recurrence, shared, desc,
            created_by=parent_ids["mom"],
        )

    # Penalties
    for name, value, desc in PENALTIES:
        db.create_penalty(fam_id, name, value, desc, created_by=parent_ids["mom"])

    # Redemption options
    option_ids: dict[str, int] = {}
    for name, unit, rate, per_child in REDEMPTIONS:
        option_ids[name] = db.create_redemption_option(
            fam_id, name, unit, rate, per_child
        )
    # Demo per-child rate: Ben gets a better pocket-money rate (8 vs 10 ₿ / R).
    db.set_kid_rate(option_ids["Pocket Money"], kid_ids["Ben"], 8.0)

    # Give Ava a starting balance so redeeming works right away.
    db.add_transaction(
        kid_ids["Ava"], 60, db.TXN_CHORE, "Chore: Wash the car (earlier)"
    )
    # A couple of pending submissions so the Approvals page isn't empty.
    db.submit_chore(kid_ids["Ava"], chore_ids["Make your bed"])
    db.submit_chore(kid_ids["Ben"], chore_ids["Wash the dishes"])

    print(f"Seeded demo family into {db.DB_PATH}\n")
    print(f"Family: {FAMILY_NAME}")
    print(f"Family code (everyone logs in with this): {FAMILY_CODE}\n")
    print("Log in with:")
    print("  Parents (username / password):")
    for name, username, password, _, is_admin in PARENTS:
        tag = "  [admin]" if is_admin else ""
        print(f"    {name:5} {username} / {password}{tag}")
    print("  Kids (name / PIN):")
    for name, pin, _, _ in KIDS:
        print(f"    {name:5} {name} / {pin}")


if __name__ == "__main__":
    main()
