---
id: '002'
title: App config wiring for dotconfig-generated env-file
status: open
use-cases: [SUC-003]
depends-on: ['001']
github-issue: ''
issue: dotconfig-migration.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# App config wiring for dotconfig-generated env-file

## Description

Update `cspawn/util/config.py` so that the application reads its configuration
from a single dotconfig-generated `.env` file rather than from the multi-file
search logic in the current `get_config()`. This decouples the app from the
secrets layout on disk and makes the config loading path identical for all
environments (devel, local-prod, prod).

The `Config` class interface (`__getattr__`, `__getitem__`, `get()`, etc.) must
remain unchanged — no callers require modification.

Precedence rule to preserve: **later-loaded files win; `os.environ` wins over
all**.

## Acceptance Criteria

- [ ] `cspawn/util/config.py`'s `get_config()` reads a single `.env` file (by
      default, `.env` in the project root or `JTL_APP_DIR`) and applies
      `os.environ` on top.
- [ ] `JTL_CONFIG_DIR` env var, when set, is respected to locate the `.env`
      file (backward compatibility for any tooling that sets it).
- [ ] The `Config` class interface is unchanged; all existing callers
      (`cspawn/auth/`, `cspawn/main/`, `cspawn/cs_docker/`, `cspawn/cli/`)
      continue to work without modification.
- [ ] App starts without error in devel mode after ticket 001 is done
      (`dotconfig load -d devel -o .env` followed by `flask run`).
- [ ] Login flow (Google OAuth or password) completes successfully in devel
      without missing-config errors.
- [ ] `DATABASE_URI` and `DOCKER_URI` resolve correctly (verified via startup
      log or a quick `cspawnctl` call).
- [ ] Optional nice-to-have: a `make dev` (or equivalent) target in the
      Makefile that runs `dotconfig load -d devel -o .env && flask run` to
      simplify the local developer workflow.

## Implementation Plan

### Approach

Rewrite the runtime path of `get_config()` in `cspawn/util/config.py`:

**New contract**:
1. Determine the `.env` file location:
   - If `JTL_CONFIG_DIR` is set, look for `.env` there.
   - Else look in `JTL_APP_DIR` (if set), then walk up from `cwd` looking for
     a `.env` file (mirrors the existing `find_parent_dir()` logic but for a
     single file).
   - Raise `FileNotFoundError` with a clear message if no `.env` is found,
     including a hint to run `dotconfig load`.
2. Load the `.env` file using `dotenv_values()` (already imported).
3. Apply `os.environ` on top (existing behaviour).
4. Return `Config(config)`.

**Keep**:
- The `Config` class — no changes needed.
- `path_interp()` — unrelated utility, keep as-is.
- `find_parent_dir()` — may be retained for backward compatibility or removed
  if no callers reference it directly outside `get_config()`.

**Remove or retire**:
- `get_config_dirs()` — no longer needed for runtime path.
- `get_config_files()` — no longer needed for runtime path.
- The synthetic `CONFIG_DIR` and `SECRETS_DIR` keys injected into the config
  dict — these are no longer meaningful (no multi-directory layout). Remove
  the injection lines; if any callers depend on them, add a note in the
  commit message and track as a follow-up.

### Files to Modify

- `cspawn/util/config.py` — rewrite `get_config()`; retire/remove
  `get_config_dirs()` and `get_config_files()`.

### Files to Optionally Modify

- `docker/Makefile` — add `dev` target:
  ```makefile
  .PHONY: dev
  dev:
      dotconfig load -d devel -o .env
      flask -A cspawn.app:app run --debug
  ```

### Testing Plan

- After running `dotconfig load -d devel -o .env` (from ticket 001), start
  the app with `flask run` and verify it reaches the login page without errors.
- Check that `get_config()` raises `FileNotFoundError` with a helpful message
  when no `.env` exists.
- Verify `os.environ` override: set `DATABASE_URI=override` in the shell and
  confirm `get_config()['DATABASE_URI']` returns the override value.
- Run existing test suite: `uv run pytest` — confirm no regressions.

### Rollback Note

The old `get_config()` can be restored from git if the new path causes issues.
The `Config` class is unchanged, so any rollback is isolated to the loader
functions.

### Documentation Updates

Add a docstring to the new `get_config()` explaining:
- The expected `.env` file location.
- How to generate it: `dotconfig load -d <deploy> [--no-export] [-e] -o .env`.
- The `JTL_CONFIG_DIR` override.
