---
id: '001'
title: "Class schema — add purge_after, purge_by, target_nodes + Alembic migration"
status: open
use-cases:
  - SUC-001
  - SUC-002
  - SUC-003
  - SUC-004
  - SUC-005
depends-on: []
github-issue: ''
issue: instructor-cluster-presize.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Class schema — add purge_after, purge_by, target_nodes + Alembic migration

## Description

Add three new nullable columns to the `Class` model in `cspawn/models.py` and
produce an Alembic migration that applies them to the existing `classes` table.

These columns are the foundation for all other sprint 005 work. Every other
ticket in this sprint depends on them existing in the DB.

**Columns to add:**
- `purge_after` — `DateTime(timezone=True)`, nullable. Start of the reap window.
  Before this time nothing for the class is reaped.
- `purge_by` — `DateTime(timezone=True)`, nullable. Hard cutoff for force-remove.
  At this time all remaining resources are removed.
- `target_nodes` — `Integer`, nullable. Computed requested cluster size from roster.

**Migration note — CRITICAL:** The app uses `db.create_all()` at startup
(`app_support.py:198`) which does NOT alter existing tables. The `migrations/`
directory has stale `versions-old/` and `versions-old-2/` subdirectories but no
live `versions/` directory. The programmer must:
1. Verify whether `flask db init` is needed to create a fresh `migrations/versions/`
   or whether an existing Alembic env can be reused.
2. Either run `flask db migrate -m "add class purge window fields"` to
   auto-generate the revision, or hand-write a minimal Alembic revision that
   executes:
   ```sql
   ALTER TABLE classes ADD COLUMN purge_after TIMESTAMP WITH TIME ZONE;
   ALTER TABLE classes ADD COLUMN purge_by TIMESTAMP WITH TIME ZONE;
   ALTER TABLE classes ADD COLUMN target_nodes INTEGER;
   ```
3. Verify `flask db upgrade` applies cleanly against the existing `classes` table
   (not just a fresh `db.create_all()` run).
4. The downgrade path should DROP all three columns.

`Class.running` is NOT removed — it remains for compatibility with existing code
paths (e.g. `class_run_state` at `classes.py:353`). Only the autoscaler stops
reading it.

## Acceptance Criteria

- [ ] `Class` model in `cspawn/models.py` declares `purge_after`, `purge_by`,
      and `target_nodes` as SQLAlchemy `Column` declarations with correct types
      and `nullable=True`.
- [ ] An Alembic revision file exists in `migrations/versions/` (or equivalent)
      that adds all three columns to the `classes` table.
- [ ] `flask db upgrade` applies the migration cleanly against an existing DB
      that has the `classes` table without these columns.
- [ ] `flask db downgrade` removes all three columns without error.
- [ ] Existing rows after migration have `NULL` for all three new fields.
- [ ] The `Class` model can be imported and instantiated with the new fields
      absent (all nullable — no schema-level `default` needed).
- [ ] `Class.running` still exists and is unmodified.

## Implementation Plan

**Files to modify:**
- `cspawn/models.py` — add three `Column` declarations to the `Class` class
  (insert after `stops_at` at line 181, before the `instructors` relationship
  at line 183).

**Files to create:**
- `migrations/versions/<revision_id>_add_class_purge_window_fields.py` — Alembic
  revision with `upgrade()` and `downgrade()`.
- If `migrations/versions/` does not exist: run `flask db init` first, or create
  the directory and `env.py` / `script.py.mako` from Flask-Migrate defaults.

**Testing plan:**
- Write a test in `tests/` that:
  1. Creates an in-memory SQLite DB with `db.create_all()` to verify the new
     columns appear on the `classes` table.
  2. Creates a `Class` row with `purge_after=None`, `purge_by=None`,
     `target_nodes=None` — verifies no error.
  3. Creates a `Class` row with all three fields set — verifies round-trip.
- Run `uv run pytest` — all existing tests must still pass.

## Verification Command

```
uv run pytest tests/ -k "class" -v
flask db upgrade  # must succeed against existing schema
```
