---
id: '001'
title: NodeOp model and migration
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
depends-on: []
github-issue: ''
issue: admin-nodes-tab-manual-swarm-node-management.md
completes_issue: false
---

# NodeOp model and migration

## Description

Add the `NodeOp` SQLAlchemy model to `cspawn/models.py` and a hand-written idempotent Alembic migration to create the `node_ops` table. This is the foundation for all node operation tracking in sprint 006 — every subsequent ticket depends on this model being importable and the table existing in the database.

`NodeOp` tracks the lifecycle of a single node operation (start or remove) launched by the admin UI. The background subprocess worker (`op-run`, ticket 002) writes status/log updates to this table; the admin routes (ticket 003) read from it for the status/log poll endpoints.

## Acceptance Criteria

- [x] `NodeOp` class is added to `cspawn/models.py` with all required columns: `id` (UUID string PK), `kind` (`'expand'` or `'remove'`), `tier` (nullable String), `target_fqdn` (nullable String), `status` (String, default `'pending'`), `exit_code` (nullable Integer), `log_path` (nullable String), `message` (nullable Text), `created_by` (nullable Integer FK → `users.id`), `created_at` (DateTime UTC), `started_at` (nullable DateTime), `finished_at` (nullable DateTime).
- [x] `from cspawn.models import NodeOp` succeeds in a Python shell without error.
- [x] Migration file `migrations/versions/v006_add_node_op_table.py` exists with `revision = "v006_add_node_op_table"` and `down_revision = "v001_add_class_purge_window_fields"`.
- [x] `flask db upgrade` on a fresh SQLite database (dev) creates the `node_ops` table without error.
- [x] `flask db upgrade` on a PostgreSQL database (or a Postgres-dialect test) creates the `node_ops` table without error; the migration is idempotent (`CREATE TABLE IF NOT EXISTS`).
- [x] `flask db downgrade` removes the `node_ops` table cleanly.
- [x] A `NodeOp` instance can be created, committed, re-queried, and updated in a unit test using the SQLite test database.

## Implementation Plan

### Approach

1. Add the `NodeOp` class to `cspawn/models.py` after the `CodeHost` class, following the same `db.Model` / `Column` / `DateTime` import pattern used by `CodeHost` and `Class`. Use `import uuid; str(uuid.uuid4())` as the default for `id` (set in Python, not via DB default, for SQLite compatibility).

2. Write `migrations/versions/v006_add_node_op_table.py` following the exact structure of `v001_add_class_purge_window_fields.py`:
   - `down_revision = "v001_add_class_purge_window_fields"` to chain the migration.
   - PostgreSQL path: `op.execute(sa.text("CREATE TABLE IF NOT EXISTS node_ops (...) "))`.
   - SQLite/other path: `op.create_table("node_ops", ...)` in a `try/except OperationalError` (table already exists is silently ignored).
   - `downgrade()`: PostgreSQL `DROP TABLE IF EXISTS node_ops`; SQLite `op.drop_table("node_ops")`.

### Files to create / modify

- `cspawn/models.py` — add `NodeOp` class; add `import uuid` at top if not already present.
- `migrations/versions/v006_add_node_op_table.py` — new migration file.

### Testing plan

- Unit test (new): create a `NodeOp` instance with `kind='expand'`, `tier='large'`, `status='pending'`; commit; re-query; assert all fields round-trip correctly. Update status to `'done'` and assert.
- Unit test (new): verify migration applies cleanly on SQLite via `flask db upgrade` (can use the existing test DB setup from sprint 005 tests).
- Run existing test suite (`uv run pytest`) to confirm no regressions in models or migrations.

### Documentation updates

None required. The model is internal infrastructure.
