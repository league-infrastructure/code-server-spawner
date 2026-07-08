---
id: '001'
title: Pin new codehosts to their placement node at creation
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-005
depends-on: []
github-issue: ''
issue: pin-codehosts-to-node-no-migration.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Pin new codehosts to their placement node at creation

## Description

Codehost services are created with `constraints=["node.role==worker"]`
(`PLACEMENT_CONSTRAINTS`, `cspawn/cs_docker/csmanager.py:444-453`) — Swarm
may place *and later reschedule* them onto any worker, forever. A node
getting into trouble (overload, heartbeat timeout, reboot) causes Swarm to
migrate its hosts elsewhere, disconnecting students, and can cascade onto
the node that receives them (the 2026-07-06 swarm2→swarm4 incident).

This ticket implements **Approach B** (confirmed decision — not open for
reconsideration; see `architecture-update.md` Steps 1 and 6): create the
service exactly as today (`node.role==worker`, unchanged), then read back
which node Swarm's own scheduler actually assigned the task to, then pin it
there via the existing, unchanged `_pin_service_to_node()`
(`cspawn/cli/node.py:159-172`). This reuses Swarm's own atomic,
capacity-aware scheduler instead of re-deriving a race-prone client-side
version of it, at the cost of one extra Swarm-triggered task reschedule
immediately after creation — hidden inside the host's normal startup
window, before a student's first `is_ready` poll typically resolves.

**Insertion point:** `CodeServerManager._new_cs_inner()`
(`cspawn/cs_docker/csmanager.py:621-711`), immediately after
`s: CSMService = self.run(**container_def)` succeeds (`csmanager.py:668`),
still inside the existing `try:` (`csmanager.py:666-684`) whose
`except docker.errors.APIError` exists solely to catch `self.run()`'s own
409-already-exists race. The new pin logic must therefore catch every
exception of its own — if it let a `docker.errors.APIError` escape (e.g.
from `svc.update()` inside `_pin_service_to_node`), that outer except would
misinterpret a pin failure as "service already exists."

**Reading the placed node:** new helper `_resolve_task_node_fqdn()` in
`cspawn/cli/node.py`, polling the raw docker-py service's `.tasks()` (not
`.container_tasks`/`.containers`, which only surface a task once it has a
*container* — a task's `NodeID` is a top-level key, assigned at scheduling
time, well before the container starts, so polling `.tasks()` lets pinning
happen as early as possible).

**Config defaults (team-lead confirmed):** `PIN_HOSTS_TO_NODE` default
`True` (on); `PIN_HOST_PLACEMENT_TIMEOUT_S` default `10.0` seconds (`NodeID`
assignment is expected to resolve in well under a second in practice —
scheduling happens before image pull/container start — 10s is a safety
margin, not a measured value).

**Failure policy (team-lead confirmed):** a failure to resolve the placed
node, or a failure inside `_pin_service_to_node` itself, is logged at
**WARNING** and must never block or fail host creation. An unpinned host
behaves exactly as it does today (Swarm may place/migrate it freely) — this
is a strict degrade-to-current-behavior, not a new failure mode.

**No retroactive backfill (team-lead confirmed, explicit out-of-scope, not
a TODO):** hosts that exist before this ticket ships are **not** touched by
it. Mass-restarting the live fleet to backfill pins was explicitly
considered and rejected (it would itself cause the disconnect churn this
sprint exists to eliminate). Pre-existing hosts get pinned the next time
they are naturally recreated (stopped and started again) or explicitly
relocated by `node rebalance` (Ticket 003) — this is a permanent design
decision, not deferred work.

## Acceptance Criteria

- [x] New `_resolve_task_node_fqdn(client, service, *, timeout=10.0, poll_interval=0.5, log=None) -> str | None`
  added to `cspawn/cli/node.py`'s existing "Shared helpers" section
  (alongside `_service_constraints`/`_pin_service_to_node`/
  `_unpin_services_from_node`). `service` is the raw docker-py `Service`
  object (`CSMService.o`, per `cs_docker/proc.py:36`). Polls
  `service.tasks()` for a task with a top-level `NodeID` key (`t.get("NodeID")`
  — the same field `count_hosts_per_node()` already reads; it is a sibling
  of `Status`/`Spec` on the task dict, not nested inside `Status`). Once
  found, resolves the hostname via
  `client.nodes.get(node_id).attrs["Description"]["Hostname"]`. Returns
  `None` and logs a WARNING if no task gets a `NodeID` within `timeout`.
  Never raises.
- [x] `_new_cs_inner()` (`csmanager.py`), immediately after
  `s: CSMService = self.run(**container_def)` (`csmanager.py:668`), gains a
  `PIN_HOSTS_TO_NODE`-gated block: lazy, function-local
  `from cspawn.cli.node import _pin_service_to_node, _resolve_task_node_fqdn`;
  resolve the placed node; call `_pin_service_to_node(s.o, node_fqdn)` if
  resolved; log a WARNING and leave the host unpinned if not. The entire
  block is wrapped in its own `try/except Exception` so nothing it does can
  ever propagate into the enclosing `except docker.errors.APIError`
  (409-recovery) handler.
- [x] The 409-recovery branch (existing service reused via
  `_get_by_username_raw`, `self.run()` never called) does **not** run the
  pin block — it lives after the `self.run()` call inside the `try`, so the
  except-handled 409 path never reaches it. Verified by test, not just code
  inspection (a retried "Start" click, or the recovery path racing another
  request's create, must not trigger a second, redundant reschedule of an
  already-pinned host).
- [x] `PIN_HOSTS_TO_NODE` config key (bool), default `True`, read via
  `getattr(self.config, "PIN_HOSTS_TO_NODE", True)` — matching the existing
  `PLACEMENT_CONSTRAINTS` idiom (`csmanager.py:447`). Setting it `False` in
  a deployment's `public.env` fully restores pre-ticket behavior: no
  placement-node poll, no pin call, `node.role==worker` is the only
  constraint, Swarm remains free to migrate.
- [x] `PIN_HOST_PLACEMENT_TIMEOUT_S` config key (float), default `10.0`,
  forwarded as `_resolve_task_node_fqdn`'s `timeout`.
- [x] A pin failure (resolve timeout, or `_pin_service_to_node` raising) logs
  at **WARNING**, never raises out of `_new_cs_inner()`, and never blocks or
  delays returning the new `(CSMService, CodeHost)` pair to the caller.
- [x] `CodeHost.node_name` is set to the resolved node fqdn before
  `db.session.commit()` in `_new_cs_inner()`, instead of staying `None`
  until the next `sync_to_db()` (today's `to_model(no_container=True)`
  always leaves `node_name=None` at creation). When resolution fails or the
  flag is off, `node_name` stays `None` exactly as today — no regression.
- [x] No retroactive backfill: no code path in this ticket touches any
  `CodeHost`/service that existed before this ticket's code runs for the
  first time on it.
- [x] Idempotent / restart-safe: re-running host start for the same user
  (e.g. a spawner restart mid-request, or a retried "Start" click hitting
  the 409-recovery path) never double-pins or fights an existing correct
  pin — relies on (a) the 409-recovery branch skipping the pin block
  entirely, and (b) `_pin_service_to_node`'s own existing
  replace-not-accumulate normalization of `node.hostname==` constraints if
  the block does run again for any reason.
- [x] Unit tests (mocked Docker client only — no live Docker/DigitalOcean
  access):
  - Direct tests of `_resolve_task_node_fqdn` in isolation, reusing the
    `_make_raw_service`/`_make_node`/`_make_manager`-style `MagicMock`
    builders already established in `test/test_node_missing.py`: resolves
    once a task carries a `NodeID`; returns `None` and logs a WARNING when
    no task is ever scheduled within `timeout`; never raises even if
    `client.nodes.get` itself raises.
  - Tests of the `_new_cs_inner()` call site, using the
    `CodeServerManager.__new__(CodeServerManager)` + manual attribute
    injection pattern already established in `test/test_stop_host.py`'s
    `_make_manager(app)` helper (bypasses `__init__`'s real Docker
    connection): pin applied when `PIN_HOSTS_TO_NODE` is on (default) and
    resolution succeeds; skipped when the flag is off; best-effort no-crash
    when resolution times out or `_pin_service_to_node` raises; **not**
    re-applied on the 409-recovery path; `CodeHost.node_name` populated
    from the resolved fqdn only when resolution succeeds.
  - A regression proving the pin, once applied, is in the exact form Swarm
    needs to refuse a migration: after the call site runs, the service's
    placement constraints contain exactly one `node.hostname==<node>` entry
    naming the node the task actually landed on (covers SUC-002 — the hard
    constraint itself is Swarm's own guarantee once correctly set; this
    locks in that this code sets it correctly).

## Implementation Plan

**Approach:** Add `_resolve_task_node_fqdn()` to `cspawn/cli/node.py`'s
"Shared helpers" section. Wire the new pin call site into
`_new_cs_inner()` immediately after `self.run(**container_def)`, gated by
`PIN_HOSTS_TO_NODE`, using a lazy function-local import from
`cspawn.cli.node` (matching the existing precedent at
`cspawn/admin/routes.py:458` and `cspawn/cs_docker/autoscale.py:649,970,1150`,
which already import `count_hosts_per_node`/`graceful_remove_node` the same
way to defer `cli/node.py`'s heavier `click`/`digitalocean`/`paramiko`
imports past Flask start-up). Add a small local `_truthy(value, default)`
boolean-config helper (mirroring `autoscale.py`'s existing `_cfg_bool()`
idiom) rather than importing from `autoscale.py` (a much heavier,
DB-backed/stateful module for a five-line boolean parse).

**Files to create/modify:**
- `cspawn/cli/node.py` — new `_resolve_task_node_fqdn()` helper.
- `cspawn/cs_docker/csmanager.py` — `_new_cs_inner()` pin call site;
  `_truthy()` helper; early `CodeHost.node_name` population.
- New test file `test/test_csmanager_pin.py` (no existing test file
  exercises `_new_cs_inner()`/`new_cs()` at all — confirmed by search of
  `test/`) for the call-site tests, following `test_stop_host.py`'s
  `_make_manager(app)` pattern (`CodeServerManager.__new__(CodeServerManager)`
  + manual `.app`/`.config`/`.client` attribute injection, `self.run`
  stubbed per-test).
- `test/test_node_unpin.py` (or the new file above) — direct
  `_resolve_task_node_fqdn()` unit tests, reusing its existing
  `MagicMock`-based service/node/manager builder style.
- Config: `PIN_HOSTS_TO_NODE`, `PIN_HOST_PLACEMENT_TIMEOUT_S` — no
  `config/*/public.env` changes required to merge (both read via
  `getattr(...)` with in-code defaults); an operator adds
  `PIN_HOSTS_TO_NODE=false` to a deployment's `public.env` only if they
  want to opt out.

**Documentation updates:** none required — no existing doc describes
today's `node.role==worker`-only placement in enough detail to need
updating for this change.

## Testing

- **Existing tests to run**: `uv run pytest --ignore=test/test_admin_coverage.py -q`
  (full suite; `test_admin_coverage.py` has known pre-existing
  PRODUCTION-env failures unrelated to this sprint — ignore).
- **New tests to write**: see the Acceptance Criteria test bullets above —
  `_resolve_task_node_fqdn` isolation tests, `_new_cs_inner()` call-site
  wiring tests (flag on/off, timeout/exception best-effort, 409-path
  skip, idempotency, `node_name` population), and the constraint-shape
  regression covering SUC-002.
- **Verification command**: `uv run pytest --ignore=test/test_admin_coverage.py -q`
