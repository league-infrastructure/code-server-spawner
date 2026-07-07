---
id: '001'
title: Pre-pull codehost images at node-expand (drain, warm, activate)
status: done
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: warm-new-nodes-prepull-and-snapshot-integration.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Pre-pull codehost images at node-expand (drain, warm, activate)

## Description

Today a freshly-joined swarm node has an empty local Docker image cache, but
Docker Swarm marks it `Availability=active` (schedulable) the instant it
joins. When the scheduler lands code-server hosts on it, they all block in
`Preparing` while the node pulls the ~1.5GB compressed code-server image from
ghcr, and Caddy 503s those students until the pull finishes (confirmed live
2026-07-06). The golden DO snapshot (id `235956540`, docker-ce 29.6.1 baked
and held) removes docker-ce install from the boot path, but deliberately
does **not** bake the code-server image, so it must be warmed at expand time
instead (see `docs/golden-node-snapshot.md`).

This ticket implements Part A of the issue: immediately after a new node's
swarm membership is confirmed (before the existing post-join
`_verify_node_provisioning` hard gate even runs), drain the node so it can
never be scheduled prematurely; after verify passes, best-effort pre-pull
the set of images any class might use; then reactivate the node. This must
be wired into **both** `expand()` (`cspawn/cli/node.py`) and `apply_plan()`'s
scale-up loop (`cspawn/cs_docker/autoscale.py`) — `apply_plan()` does not
call `expand()`, it duplicates the create/configure/join/verify sequence
inline against the same `cspawn.cli.node` helper functions, so this
behavior does not come "for free" on the autoscaler path (see
architecture-update.md's Step 1 finding).

**Reactivation must be loud on failure.** A node that gets warmed but then
fails to reactivate is silently-wasted capacity — the node sits drained
forever, invisible to the scheduler, which is a variant of the exact
capacity-loss failure mode this sprint exists to prevent. `_activate_swarm_node`
must therefore retry with backoff before giving up, and log at **ERROR**
(not WARNING) on final failure, naming the manual remedy operators need
(`docker node update --availability active <node>`).

**`NODE_PREPULL_IMAGES` is a strict union, never an override.** The
DB-derived `SELECT DISTINCT image_uri FROM class_proto` list is *always*
pre-pulled; the optional `NODE_PREPULL_IMAGES` config value only adds
extra images on top of it. There is no way to use this config key to
*reduce* pre-pull coverage below what `class_proto` implies — this was an
explicit team-lead decision to avoid a foot-gun (see architecture-update.md
Design Rationale).

## Acceptance Criteria

- [x] New `_activate_swarm_node(manager_client, node_obj, *, retries=3,
  initial_delay=2.0, log=None) -> bool` helper added to `cspawn/cli/node.py`
  (near `_drain_swarm_node`). Structurally mirrors `_drain_swarm_node`'s
  idempotent update chain (high-level `.update(availability="active")` →
  capitalized-kwarg fallback → low-level `manager_client.api.update_node(...)`
  fallback) **but** wraps the attempt in a bounded retry-with-backoff loop
  (matching the shape of the existing `_ssh_exec_retry` helper's
  retry/backoff pattern). Returns `True` on confirmed success, `False` after
  all retries are exhausted.
- [x] On final failure (all retries exhausted), `_activate_swarm_node` logs
  at **ERROR** level (not WARNING) naming the node and the manual remedy:
  `docker node update --availability active <node>`. This is a deliberate
  departure from `_drain_swarm_node`'s WARNING-level best-effort posture —
  a stuck-drained, warmed node is wasted capacity hiding in plain sight, and
  must be loud.
- [x] New `_get_prepull_images(cfg: dict) -> list[str]` helper (callable
  from within an active Flask app context): queries
  `db.session.query(ClassProto.image_uri).distinct()` and returns it
  **unioned** with the optional `NODE_PREPULL_IMAGES` comma-separated config
  value (split/stripped/de-duplicated, order-stable). The DB-derived list is
  never dropped or replaced — `NODE_PREPULL_IMAGES` can only add images, not
  remove or override any. A DB query failure is caught, logged as WARNING,
  and falls back to just the configured allowlist (or empty list) — never
  raises.
- [x] New `_prepull_images(ip: str, key_path: Path, images: list[str], *,
  timeout: float = 300.0, log=None) -> dict[str, bool]` helper: for each
  image runs `ssh root@<ip> docker pull <image>` via the extended
  `_ssh_exec(..., command_timeout=timeout)`. Catches and logs (WARNING) any
  per-image failure/timeout/non-zero exit; never raises; never aborts the
  loop over one bad image. Returns `{image: success}`.
- [x] `_ssh_exec` extended with an optional `command_timeout: float | None =
  None` parameter (default preserves every existing call site's behavior
  exactly — no existing caller passes it). When set, applies
  `.settimeout(command_timeout)` to the command channel before reading
  output/exit status, so a wedged `docker pull` raises rather than hanging
  indefinitely.
- [x] `NODE_PREPULL_TIMEOUT_S` config key (int seconds, default `300`) reads
  into `_prepull_images`'s `timeout` — tunable per deployment, no config
  file changes required to merge this ticket (opt-in via `cfg.get(...)`).
- [x] `expand()`'s post-join block (`cli/node.py`, inside the existing
  `if last_ip and last_shortname:` gate): immediately after the "Verifying
  node appears in swarm membership" loop succeeds and **before**
  `_verify_node_provisioning` runs, resolve the node object via the existing
  `_find_swarm_node(manager_client, last_fqdn, last_shortname)` and call
  `_drain_swarm_node(manager_client, node_obj, log=log)`. This closes the
  race window earlier than today (today's drain only fires reactively, on
  a verify failure).
- [x] After `_verify_node_provisioning` succeeds: resolve images once via
  `_get_prepull_images(cfg)` (inside `get_app(ctx)` + `with
  app.app_context():`, the same pattern `rebalance()` already uses), then
  `_prepull_images(last_ip, verify_key_path, images,
  timeout=cfg.get("NODE_PREPULL_TIMEOUT_S", 300), log=log)`, then
  `_activate_swarm_node(manager_client, node_obj, log=log)` — called
  regardless of individual pull outcomes (best-effort; a pull failure never
  blocks activation).
- [x] `apply_plan()`'s scale-up loop (`cs_docker/autoscale.py`) gets the
  identical sequence per node: drain immediately post-join (before
  `_verify_node_provisioning`), then (after verify succeeds) pre-pull +
  activate. Image resolution (`_get_prepull_images`) happens **once** before
  the `for tier in nodes_to_add:` loop starts, not per node. New helpers
  added to the existing `from cspawn.cli.node import (...)` lazy-import
  block (autoscale.py:970-981). If `app is None`, DB image resolution is
  skipped with a WARNING (falls back to the configured `NODE_PREPULL_IMAGES`
  allowlist only, or none) — the drain/verify/activate sequence itself is
  unaffected by a missing `app`.
- [x] **`apply_plan`'s scale-up loop has no explicit swarm-membership-wait**
  (unlike `expand()`, which polls `manager_client.nodes.list()` for up to
  300s before proceeding) — this ticket leaves that asymmetry as-is
  (out of scope; changing it risks altering existing scale-up timing
  behavior unrelated to this sprint). `apply_plan`'s early-drain call is
  placed immediately after `_join_swarm(...)` returns, matching its existing
  (unchanged) assumption that join's completion is sufficient before verify.
- [x] Ordering is asserted by tests, for both `expand()` and `apply_plan()`:
  drain is called before any pull attempt; activate is called only after all
  pull attempts complete (success or failure).
- [x] A failed/timed-out pull for one image logs a WARNING, does not raise,
  does not prevent activation, and does not block subsequent images.
- [x] A node that is warmed but fails to reactivate after exhausting
  `_activate_swarm_node`'s retries logs at ERROR level naming the node and
  the `docker node update --availability active <node>` remedy — covered by
  a dedicated test (not merely inferred from the WARNING-path tests).
- [x] Unit tests added/updated:
  - `test/test_node_provisioning_verify.py`: extend `_invoke_expand()`'s
    mock fixture with mocks for `_get_prepull_images`, `_prepull_images`,
    `_activate_swarm_node`. **Update**
    `TestExpandVerificationSuccess::test_success_exits_zero_with_unchanged_summary`
    — it currently asserts `mocks["drain_swarm_node"].assert_not_called()`,
    which no longer holds once drain fires in the happy path too. New test
    class(es) covering: drain-before-verify ordering, activate-only-after-
    pull-attempts ordering, best-effort pull-failure handling (activation
    still happens), image-list union semantics (DB images always present,
    `NODE_PREPULL_IMAGES` only adds), and the ERROR-level loud-failure path
    for exhausted activate retries.
  - `test/test_autoscale.py`: extend `TestApplyPlanScaleUpVerification` (or
    a new sibling class) with equivalent coverage for `apply_plan`'s
    scale-up loop, patched at `cspawn.cli.node.*` per this file's own
    documented convention (patching `cspawn.cs_docker.autoscale.*` would not
    intercept the lazily-imported calls).
  - New direct unit tests for `_activate_swarm_node` (including the
    retry-then-ERROR-on-exhaustion path), `_get_prepull_images` (union
    semantics, DB-failure fallback), `_prepull_images` (per-image
    best-effort, timeout), and the extended `_ssh_exec(...,
    command_timeout=...)` in isolation (mocked paramiko/DB) — implementer's
    choice of file (`test/test_node_provisioning_verify.py` or a new
    `test/test_node_prepull.py`), follow existing file-organization
    conventions.
- [x] Suite green: `uv run pytest --ignore=test/test_admin_coverage.py -q`.

## Implementation Plan

**Approach:** Add four new module-level helpers to `cspawn/cli/node.py`'s
"Shared helpers" section (near `_drain_swarm_node`/`_verify_node_provisioning`):
`_activate_swarm_node` (with retry+backoff and ERROR-on-exhaustion),
`_get_prepull_images`, `_prepull_images`, and extend `_ssh_exec`'s signature
with an optional `command_timeout`. Wire the new drain-immediately →
(existing) verify → pre-pull → activate sequence into `expand()`'s existing
`if last_ip and last_shortname:` block (`cli/node.py` ~L2641-2667) and into
`apply_plan()`'s scale-up loop (`cs_docker/autoscale.py` ~L1007-1060), adding
the new names to the existing lazy `from cspawn.cli.node import (...)` block
there. Add `NODE_PREPULL_IMAGES` / `NODE_PREPULL_TIMEOUT_S` as new optional
config keys — no config file changes required (both opt-in via `cfg.get(...)`
with in-code defaults).

**Files to create/modify:**
- `cspawn/cli/node.py` — new helpers (`_activate_swarm_node`,
  `_get_prepull_images`, `_prepull_images`), `_ssh_exec` extension,
  `expand()` post-join wiring.
- `cspawn/cs_docker/autoscale.py` — `apply_plan()` scale-up loop wiring,
  new imports.
- `test/test_node_provisioning_verify.py` — extended `_invoke_expand()`
  fixture, updated/new tests.
- `test/test_autoscale.py` — new/extended `apply_plan` scale-up tests.
- Optionally `test/test_node_prepull.py` (new) for isolated helper unit
  tests, if not folded into `test_node_provisioning_verify.py`.

**Documentation updates:** None required — `docs/golden-node-snapshot.md`
already documents this ticket's target end-state (written ahead of
implementation). If the implemented config key names or defaults diverge
from what's described there, reconcile that doc as part of this ticket.

## Testing

- **Existing tests to run**: `uv run pytest --ignore=test/test_admin_coverage.py -q`
  (full suite; `test_admin_coverage.py` has known pre-existing
  PRODUCTION-env failures unrelated to this sprint — ignore).
- **New tests to write**: see the Acceptance Criteria test bullets above —
  ordering assertions (drain-before-pull, activate-only-after-pull),
  best-effort per-image pull failure handling, `NODE_PREPULL_IMAGES` union
  semantics, and the retry-then-ERROR-on-exhaustion reactivation-failure
  path, covering both `expand()` and `apply_plan()`.
- **Verification command**: `uv run pytest --ignore=test/test_admin_coverage.py -q`
