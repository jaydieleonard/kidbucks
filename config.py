"""Central configuration & constants for KidBucks.

Pure data with no imports, so it can be imported from any layer without risk of
circular dependencies. This is the single source of truth for values that used
to be duplicated across db.py and auth.py.
"""

from __future__ import annotations

# Currency glyph for KidBucks (e.g. "125 ₿").
BUCK = "₿"

# Units written BEFORE the amount ("R1", "$5") rather than after ("30 min").
CURRENCY_UNITS = {"r", "$", "£", "€", "¥"}

# Family-code alphabet with ambiguous characters (I, L, O, 0, 1) removed.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# Transaction ledger categories.
TXN_CHORE = "chore"
TXN_PENALTY = "penalty"
TXN_REDEMPTION = "redemption"
TXN_ADJUSTMENT = "adjustment"
TXN_BONUS = "bonus"        # spontaneous reward for good behaviour
TXN_DEMERIT = "demerit"    # spontaneous one-off deduction

# Chore recurrence values and their human labels.
RECURRENCE_ONCE = "once"
RECURRENCE_OPTIONS = ["once", "daily", "weekly", "monthly"]
RECURRENCE_LABELS = {
    "once": "One-time",
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
}

# Avatars kids and parents can pick. One shared list so every screen matches.
EMOJI_CHOICES = [
    "🙂", "😀", "😎", "🦄", "🐯", "🦖", "🐶", "🐱", "🦁", "🐢", "🦋",
    "🚀", "🚗", "🏍️", "🚲", "⚽", "🏀", "🏏", "🎮", "🎸", "🎨",
    "🌟", "🌸", "🌈", "🍕",
]

# Starter content applied once when a brand-new family is created, so they don't
# land on blank Chores/Penalties/Redemption pages. Tweak freely.
DEFAULT_CHORES = [
    # name, value, recurrence, shared, description
    ("Make your bed", 5, "daily", False, "Every morning."),
    ("Wash the dishes", 10, "daily", False, "After dinner."),
    ("Tidy your room", 15, "weekly", False, ""),
    ("Take out the bins", 20, "weekly", True, "Family task — whoever does it first!"),
]
DEFAULT_PENALTIES = [
    # name, value, description
    ("Back-talk", 10, "Being rude to a grown-up."),
    ("Missed homework", 15, "Homework not done on time."),
    ("Left a mess", 5, ""),
]
DEFAULT_REDEMPTIONS = [
    # name, unit, bucks_per_unit, per_child
    ("Pocket Money", "R", 10.0, True),    # 10 KidBucks = R1, per-child rates on
    ("Screen Time", "min", 2.0, False),   # 2 KidBucks = 1 minute
]

# --- Auth ------------------------------------------------------------------
PBKDF2_ROUNDS = 200_000

# "Stay logged in" token lifetime. Note: on iOS Safari, script-set cookies are
# capped to ~7 days of inactivity regardless of this.
LOGIN_TOKEN_TTL_SECONDS = 30 * 24 * 3600
