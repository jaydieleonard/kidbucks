"""Typed domain models for the core entities.

Each is a dataclass — a documented schema with attribute access and editor
autocomplete — that ALSO behaves like the dict rows it replaces: it supports
``row["x"]``, ``row.get("x")``, ``"x" in row`` and ``dict(row)``. That lets the
whole existing codebase keep using subscript access unchanged while new code can
use attributes. Built at the repository boundary via ``from_row()``.

Only stable, full-table shapes are modelled here. Query-specific result shapes
(joins with computed columns, e.g. a kid + balance) stay plain dicts for now.
"""

from __future__ import annotations

from dataclasses import dataclass, fields


class _DictCompat:
    """Mixin: makes a dataclass a drop-in for the dict rows it replaces."""

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def get(self, key, default=None):
        return getattr(self, key, default)

    def keys(self):
        return [f.name for f in fields(self)]

    def __contains__(self, key):
        return key in self.keys()

    @classmethod
    def from_row(cls, row):
        """Build a model from a DB row (dict / sqlite3.Row), or None if no row."""
        if row is None:
            return None
        data = {}
        for f in fields(cls):
            try:
                data[f.name] = row[f.name]
            except (KeyError, IndexError):
                pass  # column absent (pre-migration) -> use the field default
        return cls(**data)


@dataclass
class User(_DictCompat):
    id: int
    family_id: int
    name: str
    role: str          # 'kid' | 'parent'
    is_admin: int      # 0 / 1
    username: str | None
    secret_hash: str
    salt: str
    emoji: str
    created_at: str
    last_seen_at: str | None = None


@dataclass
class Family(_DictCompat):
    id: int
    name: str
    code: str
    created_at: str


@dataclass
class Chore(_DictCompat):
    id: int
    family_id: int
    name: str
    description: str
    value: int
    recurrence: str    # 'once' | 'daily' | 'weekly' | 'monthly'
    shared: int        # 0 / 1
    active: int        # 0 / 1
    created_by: int | None
    created_at: str
