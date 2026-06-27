---
id: '003'
title: "Multi-size node provisioning \u2014 NODE_TIERS, tier labels, cloud-init docker\
  \ pin"
status: done
branch: sprint/003-multi-size-node-provisioning-node-tiers-tier-labels-cloud-init-docker-pin
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
issues:
- multi-size-node-provisioning
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 003: Multi-size node provisioning — NODE_TIERS, tier labels, cloud-init docker pin

## Goals

Make swarm node provisioning size-aware. An operator can provision a small or large node
by name (`--tier`), each node carries its capacity in swarm labels, and the `contract`
command safely removes only empty nodes. The cloud-init docker pin eliminates the manual
version-downgrade runbook that currently blocks reliable automated joins.

## Problem

- `expand` hardcodes a single `DO_SIZE`; there is no way to create a large vs. small node.
- No per-node capacity signal exists; `contract` cannot know how many hosts a node holds.
- `_ensure_label_on_node` hardcodes `value="true"` and cannot express `cs.tier=large`.
- `contract_node` removes the highest-serial worker with no emptiness check — it can
  drain a node with live student sessions.
- `config/cloud-init/swarm-node-init-v2.yaml` installs `docker.io` which conflicts with
  the image's docker-ce, causing major-version mismatch errors that abort automated joins.

## Solution

1. **cloud-init docker pin**: drop `docker.io`; install and hold docker-ce 27.4.1 via `runcmd`.
2. **`cspawn/cs_docker/tiers.py`**: new module with `Tier` dataclass and helpers
   (`load_tiers`, `default_tier`, `tier_by_name`, `tier_for_slug`, `default_capacity`).
3. **Config keys**: `NODE_TIERS` (JSON array), `DEFAULT_TIER`, `DEFAULT_CAPACITY` in all
   three env configs; keep `DO_SIZE` as backward-compat fallback.
4. **`_ensure_node_labels`**: new key=value label helper in `node.py`.
5. **Size-aware `expand`**: add `--tier` option; thread `tier` through `_create_droplet`
   and `_join_swarm`; stamp `cs.tier` / `cs.capacity` at join.
6. **`node label-backfill`**: new CLI subcommand for one-time labeling of existing nodes.
7. **Capacity-aware `contract`**: only remove empty nodes; prefer smallest tier, newest serial.

## Success Criteria

- `cspawnctl node expand --tier large` creates a large droplet, joins it, and labels it.
- `cspawnctl node expand` (no flag) uses `DEFAULT_TIER` and is backward-compatible.
- New nodes provisioned with updated cloud-init no longer fail the version-mismatch preflight.
- `cspawnctl node label-backfill --apply` stamps labels on existing unlabeled nodes.
- `cspawnctl node contract` refuses to remove a node with running hosts.

## Scope

### In Scope

- `config/cloud-init/swarm-node-init-v2.yaml` docker pin change.
- `cspawn/cs_docker/tiers.py` new module.
- `NODE_TIERS` / `DEFAULT_TIER` / `DEFAULT_CAPACITY` config keys in all three env configs.
- `_ensure_node_labels` key=value helper in `cspawn/cli/node.py`.
- `expand --tier` option; thread `tier` through `_create_droplet` and `_join_swarm`.
- `node label-backfill` CLI subcommand.
- Capacity-aware `contract` + extracted `_select_contract_candidate` + `_running_hosts_by_node`.

### Out of Scope

- `autoscale.py` control loop (`estimate_demand`, `node autoscale` command, cron trigger).
- `instructor-cluster-presize` (purge windows, `/cluster` route, `purge_after` timestamps).
- Tier-aware placement constraints (beyond existing `PLACEMENT_CONSTRAINTS=node.role==worker`).
- Any changes to the web API or frontend.

## Test Strategy

- Unit tests for `cspawn/cs_docker/tiers.py`: `load_tiers` with valid JSON, missing `NODE_TIERS`
  (fallback to `DO_SIZE`), invalid JSON, missing fields.
- Unit tests for `_ensure_node_labels`: verify idempotency; verify multi-key merge.
- Unit tests for `_select_contract_candidate`: verify empty-only selection, capacity sort,
  serial tiebreaker, and None when no empty nodes exist.
- Smoke test: `cspawnctl node expand --tier small` in devel config (dry-run or isolated).

## Architecture Notes

See `architecture-update.md` for the full design. Key decisions:
- Single JSON key `NODE_TIERS` (vs. parallel scalar keys) for atomic name↔slug↔capacity grouping.
- Single serial sequence for all node sizes (serial is identity, not size).
- New `_ensure_node_labels(dict)` function rather than changing `_ensure_label_on_node`
  to preserve the existing `SWARM_NODE_LABEL` boolean semantics.
- `contract` never drains a live node — empty-only gate is non-negotiable.

## GitHub Issues

None yet.

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [ ] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | cloud-init docker pin | — |
| 002 | tiers.py helper module + config keys | — |
| 003 | _ensure_node_labels + size-aware expand/join | 002 |
| 004 | node label-backfill command | 002, 003 |
| 005 | capacity-aware contract | 002, 003, 004 |

Tickets execute serially in the order listed.
