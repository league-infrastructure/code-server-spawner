---
id: '006'
title: "Admin Nodes tab — manual swarm node management"
status: planning-docs
branch: sprint/006-admin-nodes-tab-manual-swarm-node-management
use-cases:
  - SUC-001
  - SUC-002
  - SUC-003
  - SUC-004
issues:
  - admin-nodes-tab-manual-swarm-node-management.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 006: Admin Nodes tab — manual swarm node management

## Goals

Deliver a working admin UI tab for manual swarm node management: list nodes with
live host counts, start a large or small node on demand, and drain+destroy a node
— all without blocking the web request. Ship the `_ensure_priv_key` fallback so
expand works from the deployed prod container.

## Problem

The autoscaler (sprints 003–005) ships disabled by default and its demand
estimation logic is broken. The stakeholder needs direct manual control over swarm
capacity now, without waiting for autoscaler fixes.

## Solution

A `NodeOp` job model + idempotent migration tracks each operation. Admin routes
create a `NodeOp` row and launch a detached `cspawnctl node op-run <id>` subprocess
that does the actual work by invoking the existing `expand` and `stop_node` CLI
commands. A new `admin/nodes.html` template with JS polling gives the admin live
visibility into each operation's status and log. The `_ensure_priv_key` function
gains a `/root/.ssh/id_rsa` fallback so expand works in prod where
`config/secrets/id_rsa` is absent.

## Success Criteria

- Admin can list all swarm nodes with their code-server host counts.
- Admin can start a large or small node; the op runs to completion in the
  background and the new node joins the swarm with DNS.
- Admin can remove a worker node; the op drains, removes from swarm, and
  destroys the droplet.
- Live op status and log tail are visible in the UI without a page refresh.
- All of the above works in the deployed prod spawner (not just local-prod).

## Scope

### In Scope

- `NodeOp` SQLAlchemy model + `v006_add_node_op_table` Alembic migration.
- `_ensure_priv_key()` fallback to `~/.ssh/id_rsa`.
- `cspawnctl node op-run <op_id>` CLI worker command.
- 5 admin routes: list, start, remove, status poll, full log.
- `admin/nodes.html` template + JS polling.
- "Nodes" nav entry in `admin/base.html`.
- Unit tests for all new components.
- One-time prod smoke-test (start small node, remove it).

### Out of Scope

- Autoscaler changes or fixes.
- Automated retry on op failure.
- Op log pruning / retention policy (last 20 shown; no auto-delete this sprint).
- Bulk operations (start N nodes at once).

## Test Strategy

Unit tests for each component (model, CLI worker, admin routes, template
rendering). Integration tests via Flask test client with mocked Docker and
subprocess. One manual prod e2e after the `_ensure_priv_key` fix ships (start a
small node, confirm it joins + gets DNS, remove it).

## Architecture Notes

- Detached subprocess (`start_new_session=True`) chosen over Celery/threads to
  avoid new infra and survive gunicorn worker recycling.
- Fresh Docker client per list request (not the app-level client) to avoid stale
  SSH state.
- `fcntl.flock` on `{DATA_DIR}/.node-ops.lock` serializes concurrent ops (same
  idiom as `run_autoscale`).
- UUID primary key for `NodeOp` — safe to embed in URLs and subprocess args.
- See `architecture-update.md` for full design rationale.

## GitHub Issues

None.

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed
- [x] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | NodeOp model and idempotent migration | — |
| 002 | `_ensure_priv_key` prod fallback and `op-run` CLI worker | 001 |
| 003 | Admin nodes routes and `count_hosts_per_node` integration | 001, 002 |
| 004 | Nodes tab template and JS polling | 003 |
| 005 | Tests and prod smoke-test checklist | 003, 004 |

Tickets execute serially in the order listed.
