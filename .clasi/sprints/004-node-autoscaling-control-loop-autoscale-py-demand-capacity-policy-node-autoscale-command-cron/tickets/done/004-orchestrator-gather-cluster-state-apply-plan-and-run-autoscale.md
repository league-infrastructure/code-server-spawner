---
id: '004'
title: 'Orchestrator: gather_cluster_state, apply_plan, and run_autoscale'
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on:
- '002'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Orchestrator: gather_cluster_state, apply_plan, and run_autoscale

## Description

Add the three orchestrator functions to `cspawn/cs_docker/autoscale.py`. These are the
only functions in the module that perform I/O (Docker API, DO API, DB queries). They are
kept thin: all decision logic lives in the pure functions (ticket 002). Live DO/Docker
calls are not possible during implementation (DO_TOKEN expired); cover these with mocked
tests only.

## Acceptance Criteria

- [x] `gather_cluster_state(app, manager_client: DockerClient, cfg) -> tuple[list[dict], dict[str,int], int, list[dict], list[dict], dict[str,datetime]]`
      performs read-only operations:
      - Calls `count_hosts_per_node(manager_client)` (from `cspawn.cli.node`) for
        per-node Swarm task counts.
      - Calls `manager_client.nodes.list()` to get raw swarm node attrs.
      - Within `app.app_context()`, queries `CodeHost.query.all()` for live host rows
        (converted to dicts with fields `is_mia`, `is_purgeable`, `app_state`, `node_name`).
      - Within `app.app_context()`, queries `Class.query.filter_by(running=True).all()`
        for running class rows (converted to dicts with fields `running`, `stops_at`,
        `students` as a list of student objects or count).
      - Builds `empty_since` dict: for each node with `host_counts[short] == 0`, records
        `datetime.now(timezone.utc)` if not already tracked (carried across cycles via a
        module-level dict; reset when `running_hosts > 0`). Nodes that become non-empty
        are removed from `empty_since`.
      - Returns `(node_dicts, host_counts, pending_count, class_rows, host_rows, empty_since)`.
      - Is **read-only**: no mutations to DB, Swarm, or DO.

- [x] `apply_plan(ctx, plan: ScalePlan, cfg, *, dry_run: bool) -> ApplyResult`
      where `ApplyResult` is a simple dataclass `(added: int, removed: int, purged: bool, dry_run: bool, errors: list[str])`:
      - When `dry_run=True`: logs the plan and returns immediately with no side effects.
      - Scale-up path (when `plan.add_large + plan.add_small > 0`):
        - For each node to add: resolve `tier` from `load_tiers(cfg)` (large or small),
          call `_create_droplet(ctx, tier=tier, ...)`, `_configure_node(ctx, ...)`,
          `_join_swarm(ctx, ..., tier=tier)`. These are imported from `cspawn.cli.node`.
        - Docker version mismatch raises `ClickException`: catch it, log as error, append
          to `errors`, and return without proceeding to remaining nodes.
      - Scale-down path (when `plan.remove_nodes` is non-empty):
        - `purge_first=True`: call `app.csm.sync(check_ready=True)` within app context.
        - For each fqdn in `plan.remove_nodes`:
          - Re-check `count_hosts_per_node(manager_client)[short] == 0` immediately before
            draining. If hosts appeared (race condition), log warning and skip this node.
          - Call `graceful_remove_node(ctx, manager_client, mgr, fqdn, dry_run=False, log=log)`
            (imported from `cspawn.cli.node`).

- [x] `run_autoscale(ctx, *, dry_run: bool, force: bool, up_only: bool | None = None) -> ApplyResult`
      is the single entry point:
      1. Read `AUTOSCALE_ENABLED` from config; if `false`, log "autoscale disabled" and
         return empty `ApplyResult`.
      2. If `AUTOSCALE_DRY_RUN` config is `true`, override `dry_run=True` (cannot un-set
         global dry-run via CLI flag alone; the config key is the global override).
      3. Acquire `fcntl.flock` on a file at `cfg.get("DATA_DIR", "/tmp") + "/.autoscale.lock"`.
         Use `LOCK_EX | LOCK_NB`. If lock not acquired, log "previous cycle still running"
         and return empty `ApplyResult`.
      4. Call `gather_cluster_state(app, manager_client, cfg)`.
      5. Call `assess_cluster(node_dicts, host_counts, pending, cfg)` to get `ClusterState`.
      6. Call `estimate_demand(class_rows, host_rows, cfg)` to get demand.
      7. Call `build_plan(state, demand, cfg, now, empty_since)`, passing `force=force`
         to optionally bypass cooldown in `plan_scale_down` (when `force=True`, ignore
         `AUTOSCALE_SCALEDOWN_COOLDOWN_MIN`).
      8. If `up_only=True`, zero out `plan.remove_nodes`. If `up_only=False` (down-only),
         zero out `plan.add_large` and `plan.add_small`.
      9. Emit structured log line (demand, capacity, load, deficit, excess, plan summary,
         reason) via `get_logger(ctx)`.
      10. Call `apply_plan(ctx, plan, cfg, dry_run=dry_run)`.
      11. Release flock (in a `finally` block so lock is always released on exception).
      12. Return `ApplyResult`.

- [x] `empty_since` persistence: module-level `dict[str, datetime]` in `autoscale.py` that
      accumulates across cron invocations within the same process. (Each `cspawnctl` invocation
      is a fresh process, so this dict starts empty each cycle. The `SCALEDOWN_COOLDOWN_MIN`
      must be understood accordingly: cooldown is tracked across cycles only if the same
      process runs multiple cycles, which it does not in cron mode. For cron use, the
      cooldown works by re-checking state each cycle — a node that was empty last cycle and
      is still empty this cycle does NOT automatically satisfy cooldown unless we track it
      in persistent storage.) **Decision required from team-lead / stakeholder**: should
      `empty_since` be persisted in a JSON file under `DATA_DIR`? If not, cooldown is
      effectively disabled in cron mode (every cron run sees a fresh `empty_since = {}`).
      For now, implement with a JSON sidecar file at `{DATA_DIR}/.autoscale_state.json`
      that persists `empty_since` across runs. The file is written atomically (write to
      temp then rename).

- [x] Thin mocked tests in `test/test_autoscale.py`:
      - `gather_cluster_state` with `MagicMock` Docker client (nodes.list returns two fake
        node dicts, services.list returns empty) and a minimal Flask app with an in-memory
        DB. Verify it returns the correct tuple structure without errors.
      - `apply_plan` with `dry_run=True` and a non-trivial `ScalePlan`: verify no Docker
        or DO calls are made (use `MagicMock` and assert not called).
      - `run_autoscale` with `AUTOSCALE_ENABLED=false`: verify it returns early without
        calling `gather_cluster_state`.

- [x] `uv run pytest` passes.

## Implementation Plan

### Approach

Add the three orchestrator functions to the bottom section of `autoscale.py`, clearly
separated from the pure functions by a `# --- Orchestrator (side-effecting) ---` comment.
Import `docker`, `digitalocean`, and `flask` only inside these functions (lazy import) so
the pure-function portion of the module remains importable without those packages (for
unit-test isolation).

Alternatively: lazy imports at function entry. Either approach is acceptable; lazy import
at function body is safest for test isolation.

### `empty_since` persistence

Write `{DATA_DIR}/.autoscale_state.json` with structure:
```json
{"empty_since": {"swarm3.dojtl.net": "2026-06-26T10:00:00+00:00"}}
```
Load at `run_autoscale` start; update and write atomically after `gather_cluster_state`.
Remove entries for nodes that are no longer in the cluster or have `running_hosts > 0`.

### Files to Modify

- `cspawn/cs_docker/autoscale.py` — add orchestrator functions.
- `test/test_autoscale.py` — add mocked orchestrator tests.

### Testing Plan

Pure function tests (ticket 003) are already in place. This ticket adds thin mocked tests
for the orchestrator. No live Docker or DO calls in CI.

Run: `uv run pytest test/test_autoscale.py -v` then `uv run pytest`.

### Documentation Updates

Update module docstring in `autoscale.py` to document the `empty_since` persistence file
and the `AUTOSCALE_*` config keys read by the orchestrator.
