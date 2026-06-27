---
id: '002'
title: _ensure_priv_key prod fallback and op-run CLI worker
status: done
use-cases:
- SUC-002
- SUC-003
- SUC-004
depends-on:
- '001'
github-issue: ''
issue: ''
completes_issue: false
---

# _ensure_priv_key prod fallback and op-run CLI worker

## Description

Two changes to `cspawn/cli/node.py`:

1. **`_ensure_priv_key()` prod fallback**: The current implementation (`node.py:762`) raises immediately if `config/secrets/id_rsa` is absent. In the deployed prod container, `config/secrets/` is empty but the working swarm key lives at `/root/.ssh/id_rsa`. Add a fallback: if the primary path is absent, try `/root/.ssh/id_rsa` before raising. This unblocks `expand` in prod without affecting local-prod (which has `config/secrets/id_rsa`).

2. **`cspawnctl node op-run <op_id>` command**: The new subprocess worker target. It loads the `NodeOp` from the DB, acquires a flock to serialize ops, tees output to a log file, invokes the appropriate existing command (`expand` or `stop_node` via `ctx.invoke`), and updates the `NodeOp` status on completion. This is the only component that runs as a detached subprocess; all real node management work is delegated to the already-tested `expand` and `stop_node` commands.

## Acceptance Criteria

- [x] `_ensure_priv_key()` returns the key paths from `/root/.ssh/id_rsa` (and `/root/.ssh/id_rsa.pub` if it exists) when `config/secrets/id_rsa` is absent.
- [x] `_ensure_priv_key()` raises `ClickException` with a message naming both paths checked when neither exists.
- [x] `_ensure_priv_key()` behavior is unchanged when `config/secrets/id_rsa` exists (primary path still returned).
- [x] `cspawnctl node op-run --help` shows the command exists and accepts `<op_id>`.
- [x] Running `cspawnctl node op-run <uuid>` for a `kind='expand'` `NodeOp` (with `ctx.invoke` mocked) sets `status='running'` then `status='done'`; `log_path` file is created; `started_at` and `finished_at` are set.
- [x] Running `cspawnctl node op-run <uuid>` for a `kind='remove'` `NodeOp` (with `ctx.invoke` mocked) follows the same lifecycle.
- [x] If `ctx.invoke` raises an exception, `status` is set to `'failed'`, `exit_code=1`, and `message` contains the exception text.
- [x] The `{DATA_DIR}/.node-ops.lock` file is created and the flock is acquired/released correctly (verify with a unit test that holds the lock and confirms a second invocation fails immediately).
- [x] Log output from the invoked command is written to `{DATA_DIR}/node-ops/<id>.log`.

## Implementation Plan

### Approach

**`_ensure_priv_key()` fallback** (minimal change):
Replace the current raise with a two-step check:
1. `priv_key_path = workspace_root / "config" / "secrets" / "id_rsa"` — existing primary.
2. If not found: `priv_key_path = Path("/root/.ssh/id_rsa")`.
3. If still not found: raise `ClickException` naming both paths.
4. Derive `pub_key_path` as `priv_key_path.with_suffix(".pub")` (may not exist; callers already handle absent pub key gracefully via `_collect_do_ssh_keys`).

**`op-run` command** (new Click command under the `node` group):

```
@node.command(name="op-run")
@click.argument("op_id")
@click.pass_context
def op_run(ctx, op_id: str):
    """Execute a pending NodeOp by ID (called as a detached subprocess by the admin UI)."""
```

Implementation steps within the command:
1. Obtain Flask app context via `get_app(ctx)` (same pattern used by other node commands).
2. Inside `app.app_context()`: load `NodeOp` by `op_id`; if not found, raise `ClickException`. Set `status='running'`, `started_at=datetime.now(utc)`; commit.
3. Compute `log_path = Path(cfg.get("DATA_DIR", "/tmp")) / "node-ops" / f"{op_id}.log"`. Create directory. Open log file for writing.
4. Redirect `sys.stdout` and `sys.stderr` to the log file (or use `os.dup2` for subprocess-level redirection).
5. Acquire flock: `lock_path = Path(data_dir) / ".node-ops.lock"`. Open and `fcntl.flock(LOCK_EX | LOCK_NB)`. On `BlockingIOError`, set `status='failed'`, `message='another op is running'`; exit.
6. Inside try/finally (release lock in finally):
   - If `op.kind == 'expand'`: `ctx.invoke(expand, tier_name=op.tier)`.
   - If `op.kind == 'remove'`: `ctx.invoke(stop_node, node_spec=op.target_fqdn, force=False, dry_run=False)`.
   - On success: set `status='done'`, `exit_code=0`, `finished_at`.
   - On exception: set `status='failed'`, `exit_code=1`, `message=str(exc)`, `finished_at`; re-raise or log.
7. Commit final status.

### Files to modify

- `cspawn/cli/node.py`:
  - Modify `_ensure_priv_key()` (around line 762) to add the `/root/.ssh/id_rsa` fallback.
  - Add `op_run` Click command after the existing `stop_node` command.

### Testing plan

- Unit test: `_ensure_priv_key` with `config/secrets/id_rsa` present — returns primary path (monkeypatch `find_parent_dir`).
- Unit test: `_ensure_priv_key` with primary absent, `/root/.ssh/id_rsa` present (monkeypatch Path.exists) — returns fallback path.
- Unit test: `_ensure_priv_key` with both absent — raises `ClickException` with message naming both paths.
- Unit test: `op-run` expand lifecycle — mock `ctx.invoke`, assert `NodeOp` status transitions pending→running→done; log file created; timestamps set.
- Unit test: `op-run` remove lifecycle — same structure with `kind='remove'`.
- Unit test: `op-run` failure path — mock `ctx.invoke` to raise; assert `status='failed'`, `message` populated.
- Unit test: flock serialization — hold the lock in a thread; invoke `op-run` from test; assert it exits immediately with `status='failed'` and message 'another op is running'.
- Run `uv run pytest` to confirm no regressions.

### Documentation updates

None required. The fallback behavior is described in `architecture-update.md`.
