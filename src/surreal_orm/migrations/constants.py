"""
Constants shared across the migrations package.

``state.py`` is pure data: it computes a diff and knows nothing about applying
one. Reaching into ``executor.py`` for a single string dragged the connection
manager into that dependency, so the names both modules need live here.
"""

#: Table where the executor records which migrations have been applied.
MIGRATIONS_TABLE = "_surreal_orm_migrations"
