"""Display formatting helpers — one home for KidBucks, units and dates.

Previously fmt_bucks lived in auth.py, fmt_units/is_currency_unit in db.py, and
date formatting was re-implemented ad hoc in several view files. Consolidated
here so every screen formats identically.
"""

from __future__ import annotations

from datetime import datetime

from config import BUCK, CURRENCY_UNITS


def fmt_bucks(amount) -> str:
    """Render an amount of KidBucks, e.g. 125 -> '125 ₿'."""
    return f"{amount:,} {BUCK}"


def is_currency_unit(unit: str) -> bool:
    return (unit or "").strip().lower() in CURRENCY_UNITS


def fmt_units(amount, unit: str) -> str:
    """Format a redemption quantity with its unit the natural way.

    Currency symbols prefix the amount ('R', '$' -> 'R5', '$5'); everything else
    is a suffix ('30 min'). Whole numbers show without a trailing '.0'.
    """
    a = f"{amount:g}"
    u = (unit or "").strip()
    if u.lower() in CURRENCY_UNITS:
        return f"{u}{a}"
    return f"{a} {u}"


def fmt_dt(iso: str, pattern: str = "%d %b %Y, %H:%M") -> str:
    """Format an ISO timestamp for display, falling back to a safe slice."""
    try:
        return datetime.fromisoformat(iso).strftime(pattern)
    except (ValueError, TypeError):
        return (iso or "")[:16].replace("T", " ")
