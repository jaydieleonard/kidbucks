"""Data-access repositories, one module per aggregate.

Each repository owns the SQL for its aggregate and imports only from the
engine (connection/cache) plus config/formatting/models — never from db.py.
db.py re-exports them so callers keep using the flat db.* interface.
"""
