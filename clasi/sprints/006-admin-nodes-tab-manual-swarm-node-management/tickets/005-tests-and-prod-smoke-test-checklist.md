---
id: "005"
title: "Tests and prod smoke-test checklist"
status: open
use-cases:
  - SUC-001
  - SUC-002
  - SUC-003
  - SUC-004
depends-on:
  - "003"
  - "004"
github-issue: ""
issue: admin-nodes-tab-manual-swarm-node-management.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# 005 — Tests and prod smoke-test checklist

## Description

Write the pytest test suite for sprint 006 and document the minimal manual
prod smoke-test. Tickets 001–004 include unit tests for their own components;
this ticket adds integration-level coverage and the prod e2e checklist.

**Note on DO_TOKEN**: The DO_TOKEN is currently valid. Live e2e tests against
real DigitalOcean are possible but each creates/destroys a real droplet
($$$). Keep live verification minimal — one start + one remove — and always
clean up. The primary goal is confirming the full flow works in prod after
the `_ensure_priv_key` fallback ships.

## Acceptance Criteria

### Unit / integration tests (automated)
- [ ] `NodeOp` model round-trip: create with `kind='expand'`, `tier='large'`,
      `status='pending'`; commit; re-query; assert all fields. Update
      `status='done'`, `exit_code=0`; assert persisted. (`test_node_op_model.py`)
- [ ] Migration applies cleanly: `flask db upgrade` creates `node_ops` table in
      SQLite test DB. (`test_node_op_migration.py` or inline in model test)
- [ ] `_ensure_priv_key()` with primary key present returns primary.
      (`test_node_key_fallback.py`)
- [ ] `_ensure_priv_key()` with primary absent and fallback present returns
      fallback. (monkeypatch `find_parent_dir` to a tmp dir without the key;
      monkeypatch `Path.home()` to point to a tmp dir with a fake key)
- [ ] `_ensure_priv_key()` with neither key present raises `ClickException`
      naming both paths.
- [ ] `op-run` expand lifecycle: mock `ctx.invoke(expand, ...)`; assert
      `NodeOp` transitions `pending→running→done`; log file created;
      `started_at`/`finished_at` set. (`test_op_run.py`)
- [ ] `op-run` remove lifecycle: mock `ctx.invoke(stop_node, ...)`; same
      lifecycle assertions.
- [ ] `op-run` failure path: mock `ctx.invoke` to raise; assert
      `status='failed'`, `message` set.
- [ ] `op-run` lock contention: hold flock in test thread; run `op-run`; assert
      it exits immediately with `status='failed'` and "another op is running"
      message.
- [ ] `GET /admin/nodes` returns 200 with mocked Docker. (`test_admin_nodes.py`)
- [ ] `GET /admin/nodes` redirects non-admin.
- [ ] `POST /admin/nodes/start` valid tier: mocked `Popen`; assert `NodeOp`
      created + `Popen` called with correct args.
- [ ] `POST /admin/nodes/start` invalid tier: no `NodeOp`, no `Popen`.
- [ ] `POST /admin/nodes/remove` worker: mocked Docker + `Popen`; assert
      `NodeOp(kind='remove')` created.
- [ ] `POST /admin/nodes/remove` manager: refused, no `NodeOp`, no `Popen`.
- [ ] `GET /admin/nodes/op/<id>/status`: correct JSON shape; `log_tail` from
      temp file.
- [ ] `GET /admin/nodes/op/<id>/log`: plain text response.
- [ ] Template renders `nodes.html` without error when `node_rows=[]`.
- [ ] Full regression: `uv run pytest` passes with no existing tests broken.

### Manual prod smoke-test (one-time, after deploy)

Pre-conditions:
1. New code is deployed to the prod container (`codeserver_codeserver`).
2. `flask db upgrade` has been run (migration applied).
3. `/root/.ssh/id_rsa` in the prod container has its public key registered
   as a DigitalOcean SSH key (verify: `cspawnctl node info` shows SSH key).

Procedure:
- [ ] Log into the prod admin UI; navigate to the Nodes tab.
- [ ] Verify the node table loads with current swarm members and host counts.
- [ ] Click "Start small" (or whichever tier is smaller/cheaper).
- [ ] Observe the Operations panel: op shows `running`; log tail updates every 2s.
- [ ] Wait ~90s: confirm a new `swarmN` node appears in the node table.
- [ ] Verify its DNS A record exists: `dig swarmN.jtl.codes` (or equivalent).
- [ ] Click Remove on the new node; confirm JS dialog appears.
- [ ] Confirm the op runs to completion: node disappears from the table.
- [ ] Verify the droplet is destroyed in the DigitalOcean console.
- [ ] Check for any stuck `NodeOp` rows with `status='running'` after cleanup.

## Implementation Plan

### Files to create
- `tests/test_node_op_model.py` — NodeOp model + migration tests.
- `tests/test_node_key_fallback.py` — `_ensure_priv_key()` fallback tests.
- `tests/test_op_run.py` — `op-run` command lifecycle tests.
- `tests/test_admin_nodes.py` — admin routes + template tests (may consolidate
  with `test_admin_nodes_routes.py` from ticket 003 if that file was created).

### Approach

Use the existing Flask test client pattern from other test files in `tests/`.
For Docker-dependent tests, use `unittest.mock.MagicMock` and
`pytest.monkeypatch` to mock `docker.DockerClient` and `subprocess.Popen`.

For `op-run` flock tests: use `threading.Thread` to hold the lock file open
while invoking the CLI command via Click's `CliRunner`.

For `_ensure_priv_key` path tests: use `tmp_path` fixtures to create fake key
files and monkeypatch `find_parent_dir` to return `tmp_path`.

### Testing plan

All tests listed in Acceptance Criteria above. Run with:
```
uv run pytest tests/test_node_op_model.py tests/test_node_key_fallback.py \
    tests/test_op_run.py tests/test_admin_nodes.py -v
```

Full regression:
```
uv run pytest
```

### Documentation updates

Update `clasi/sprints/006-admin-nodes-tab-manual-swarm-node-management/sprint.md`
with the prod smoke-test result (pass/fail) after the manual e2e.
