---
id: '002'
title: Add droplet_id column to node_ops (schema + model)
status: open
use-cases:
- SUC-004
depends-on: []
github-issue: ''
issue: nodeop-orphaned-on-container-restart.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add droplet_id column to node_ops (schema + model)

## Description

This is the foundation ticket for the NodeOp-orphan-recovery half of this
sprint (see `clasi/issues/nodeop-orphaned-on-container-restart.md`). It adds
one nullable column so a later ticket can record which DigitalOcean droplet
an `expand` `NodeOp` created, and so an interrupted op's message can name
that specific droplet instead of just saying "something might be wrong."

`NodeOp.target_fqdn` (`cspawn/models.py:525`) already exists but is
documented/used only for `remove` ops today. This ticket:

1. Adds a new nullable `droplet_id` column (`Integer`) to `node_ops`, via
   an Alembic migration following the existing dialect-branching, idempotent
   style already established in `migrations/versions/v006_add_node_op_table.py`
   (PostgreSQL: `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`; SQLite/tests:
   `op.add_column` wrapped in `try/except OperationalError`).
2. Adds `droplet_id = Column(Integer, nullable=True)` to the `NodeOp` model
   in `cspawn/models.py`, with a comment explaining its purpose.
3. Updates `target_fqdn`'s existing comment to document its dual use: FQDN
   for `remove` ops (existing, unchanged), or the created droplet's FQDN
   for `expand` ops (new — populated by a later ticket, not this one).

This ticket does **not** populate either column from application code — it
only adds the schema and model attribute. Population is ticket 004
(`Record created droplet id/fqdn on its triggering NodeOp`), which depends
on this one.

**No `status` column change is needed.** `status` is a free-text
`String(16)` (not a DB enum/check constraint); the new `'interrupted'`
value used by ticket 003 is 11 characters and fits without a migration.

See `architecture-update.md` Step 3 (M2), Step 5 (`migrations/versions/`),
and Step 6 ("sweep composes the orphan note from existing/soon-to-exist
columns") for the full design and rationale.

## Acceptance Criteria

- [ ] New Alembic migration in `migrations/versions/`, `down_revision =
  "v006_add_node_op_table"`, adding `node_ops.droplet_id INTEGER NULL`.
- [ ] Migration is idempotent and dialect-branched like
  `v006_add_node_op_table.py`: PostgreSQL uses `ALTER TABLE node_ops ADD
  COLUMN IF NOT EXISTS droplet_id INTEGER`; SQLite/other uses
  `op.add_column(...)` wrapped in `try/except OperationalError` (table
  already has the column).
- [ ] `downgrade()` drops the column: PostgreSQL `ALTER TABLE node_ops DROP
  COLUMN IF EXISTS droplet_id`; SQLite `op.drop_column("node_ops",
  "droplet_id")`.
- [ ] `NodeOp` model (`cspawn/models.py`) gains `droplet_id = Column(Integer,
  nullable=True)` with an explanatory comment.
- [ ] `NodeOp.target_fqdn`'s existing comment is updated to note its dual
  use (remove target vs. expand's created droplet).
- [ ] Existing rows (and any row created without setting `droplet_id`)
  read back as `None` — no default value, no backfill.
- [ ] Applying the migration to a fresh SQLite/in-memory test database
  (matching `test/test_node_op_cli.py`'s `app_db` fixture pattern) and then
  running `db.create_all()` produces a `node_ops` table with the new
  column, confirmed via a direct attribute read on a constructed `NodeOp`
  row.

## Implementation Plan

**Approach**: Copy `migrations/versions/v006_add_node_op_table.py`'s
dialect-branching, idempotent structure exactly, scoped to a single
`ADD COLUMN`/`add_column` instead of a whole new table. Update the model in
the same commit so schema and ORM mapping never drift.

**Files to create/modify**:
- New `migrations/versions/v0XX_add_node_op_droplet_id.py` (pick the next
  free `vNNN` following this repo's `v001`/`v006` naming; confirm current
  head via `down_revision` chain in `migrations/versions/` before naming).
- `cspawn/models.py` — add `droplet_id` column to `NodeOp`; update
  `target_fqdn`'s comment.

**Testing plan**:
- Follow `test/test_node_op_cli.py`'s `app_db` fixture (`Flask` app +
  `sqlite:///:memory:` + `db.create_all()`) — this exercises the SQLAlchemy
  model directly (not the Alembic migration script itself, which isn't run
  against SQLite in this repo's test suite; `db.create_all()` derives the
  schema from the model, so a model/migration mismatch would only surface
  in a real Postgres migration run — note this in the PR description if
  the migration and model diverge for any reason).
- New/extended test in `test/test_node_op_cli.py` or a new
  `test/test_node_op_schema.py`: construct a `NodeOp(kind="expand", ...)`,
  set `droplet_id=12345`, commit, re-query, assert the value round-trips.
- New test: a `NodeOp` constructed without `droplet_id` reads back `None`.
- If feasible in this environment, sanity-check the migration file's
  syntax (`python -c "import migrations.versions.v0XX_..."`) — a full
  `alembic upgrade`/`downgrade` cycle against a real Postgres instance is
  out of scope for this ticket's automated tests (no live Postgres in this
  repo's test suite) but should be spot-checked manually if a local
  Postgres is available before merge.

**Documentation updates**: None beyond the model/migration comments
described above.

## Testing

- **Existing tests to run**: `uv run pytest test/test_node_op_cli.py
  test/test_node_op_model.py`
- **New tests to write**: `droplet_id` round-trip test (set/commit/re-query)
  and default-`None` test, in `test/test_node_op_cli.py` or a new
  `test/test_node_op_schema.py`.
- **Verification command**: `uv run pytest`
