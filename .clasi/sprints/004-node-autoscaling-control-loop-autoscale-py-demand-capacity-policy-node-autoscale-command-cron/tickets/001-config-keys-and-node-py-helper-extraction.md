---
id: '001'
title: Config keys and node.py helper extraction
status: open
use-cases:
  - SUC-001
  - SUC-002
  - SUC-003
depends-on: []
github-issue: ''
issue: node-autoscaling-control-loop.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Config keys and node.py helper extraction

## Description

This is Phase 0 of the autoscaling rollout: add the `AUTOSCALE_*` config keys with safe
defaults to all three `public.env` files, and extract two reusable helpers from `node.py`
so that the new `autoscale.py` module can call them without duplication. No behavior changes
to any existing CLI command.

## Acceptance Criteria

- [ ] `AUTOSCALE_ENABLED=false` added to `config/prod/public.env`, `config/local-prod/public.env`,
      and `config/devel/public.env`.
- [ ] `AUTOSCALE_DRY_RUN=true` added to all three `public.env` files.
- [ ] The following `AUTOSCALE_*` keys are present in all three files:
      `AUTOSCALE_HEADROOM=2`, `AUTOSCALE_ROSTER_FRACTION=0.8`,
      `AUTOSCALE_MAX_ADD_PER_CYCLE=2`, `AUTOSCALE_MAX_REMOVE_PER_CYCLE=1`,
      `AUTOSCALE_SCALEDOWN_COOLDOWN_MIN=30`, `AUTOSCALE_MIN_WORKER_NODES=1`,
      `AUTOSCALE_DEFAULT_CAPACITY=6`.
- [ ] `count_hosts_per_node(client: DockerClient) -> dict[str, int]` is a module-level
      function in `cspawn/cli/node.py`. Its implementation is the same query as
      `_running_hosts_by_node` (already there at lines 49-71) — rename/alias or unify.
      The `hosts` command and `contract` command continue to use the same underlying
      logic without regression.
- [ ] `graceful_remove_node(ctx, manager_client, mgr, fqdn, *, dry_run: bool, log) -> None`
      is a module-level function in `cspawn/cli/node.py`. It encapsulates the drain →
      wait-tasks-drained → remove-swarm-node → destroy-droplet sequence extracted from
      `stop_node`'s non-force path (current lines ~1739-1791). `stop_node` calls
      `graceful_remove_node` rather than containing the logic inline; behavior is identical.
- [ ] `uv run pytest` passes with no regressions (existing `test_node_contract.py`,
      `test_node_labels.py`, `test_node_backfill.py`, `test_tiers.py`).

## Implementation Plan

### Approach

Config keys first (no code risk), then the two helper extractions one at a time.

### Files to Modify

- `config/prod/public.env` — add `AUTOSCALE_*` block at end.
- `config/local-prod/public.env` — same block.
- `config/devel/public.env` — same block.
- `cspawn/cli/node.py`:
  1. The existing `_running_hosts_by_node` at lines 49-71 already does exactly what
     `count_hosts_per_node` needs. The cleanest approach: rename `_running_hosts_by_node`
     to `count_hosts_per_node` (public name, no leading underscore) and update all
     callers in `node.py` (`hosts` command at line 102, `_select_contract_candidate` at
     line 2074, `_select_drain_candidate` at line 2125).
  2. Extract `graceful_remove_node` from `stop_node` (lines ~1739-1791). The extracted
     function needs the same parameters it uses inline: `ctx`, `manager_client`,
     `mgr` (DO Manager), `fqdn`, `dry_run`, `log`. Replace the inline block in
     `stop_node` with a call to `graceful_remove_node`.

### Testing Plan

- Run the full test suite: `uv run pytest`.
- Verify `test_node_contract.py` still passes (uses `_select_contract_candidate` which
  calls `count_hosts_per_node`).
- No new tests for this ticket — the extractions are behavior-preserving; the correctness
  of `count_hosts_per_node` is verified by the existing contract tests, and
  `graceful_remove_node` is pure refactor of existing code.

### Documentation Updates

Add a comment block in `node.py` above `count_hosts_per_node` and `graceful_remove_node`
marking them as shared helpers used by both the CLI commands and `autoscale.py`.
