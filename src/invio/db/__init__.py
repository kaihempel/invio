"""Persistence layer: SQLAlchemy models, engine/session helpers and Alembic migrations.

Modules: ``models`` (tables), ``types`` (``UTCDateTime``, ``utcnow``), ``session`` (engine
factory, URL normalisation, ``redact``, ``session_scope`` unit of work), ``repositories``
(flush-only access per table), ``migrate`` (programmatic Alembic access) and
``migrations`` (the packaged Alembic environment and revisions).
"""
