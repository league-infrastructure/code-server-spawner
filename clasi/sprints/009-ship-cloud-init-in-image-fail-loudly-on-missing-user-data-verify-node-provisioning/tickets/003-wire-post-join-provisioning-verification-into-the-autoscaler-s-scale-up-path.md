---
id: '003'
title: Wire post-join provisioning verification into the autoscaler's scale-up path
status: open
use-cases: [SUC-003]
depends-on: ['002']
github-issue: ''
issue: container-node-expand-missing-cloud-init.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Wire post-join provisioning verification into the autoscaler's scale-up path

## Description

Ticket 002 adds post-join provisioning verification to the manual
`cspawnctl node expand` CLI path. The automated autoscaler scale-up path
(`cspawn/cs_docker/autoscale.py::apply_plan`) creates, configures, and
joins nodes using the exact same `cli.node` primitives
(`_create_droplet`, `_configure_node`, `_join_swarm` — imported locally at
`cs_docker/autoscale.py:960-965` to avoid a module-level circular import)
but has no equivalent verification step. An unattended, cron-triggered
scale-up cycle is exactly the scenario where a silently-defective node is
most dangerous — there's no operator watching the CLI output to notice a
problem.

This ticket wires the same `_verify_node_provisioning` (from ticket 002)
into `apply_plan`'s scale-up loop (`cs_docker/autoscale.py:958-1022`),
after `_join_swarm(...)` succeeds for each planned node. Unlike `node
expand` (which aborts the whole command on failure), a scale-up cycle may
be provisioning several nodes across tiers in one pass — a single bad node
should not abort provisioning for the rest. On verification failure: log
`ERROR`, record the failure in `ApplyResult.errors`, drain the node
(best-effort), do **not** count it toward `result.added`, and `continue` to
the next planned node.

See `clasi/issues/container-node-expand-missing-cloud-init.md` and this
sprint's `architecture-update.md` Step 6 ("Autoscaler continues the batch
on a per-node verification failure, but still `break`s on
`ClickException`/generic exceptions") for the rationale distinguishing a
per-node verification failure (continue) from a systemic failure like a bad
DO token (break, unchanged).

## Acceptance Criteria

- [ ] `apply_plan`'s scale-up loop (`cs_docker/autoscale.py:991-1022`)
  imports `_verify_node_provisioning`, `_expected_docker_version`,
  `_find_swarm_node`, `_drain_swarm_node` from `cspawn.cli.node`, alongside
  the existing `_create_droplet`, `_configure_node`, `_join_swarm`,
  `_get_next_serial` import (`cs_docker/autoscale.py:960-965`).
- [ ] After `_join_swarm(ctx, fqdn, _client, docker_uri, tier=tier)`
  succeeds for a tier (`cs_docker/autoscale.py:1009`), calls
  `_verify_node_provisioning(ip, key_path, expected_docker_version=_expected_docker_version(cfg), log=log)`
  using a fresh `_ensure_priv_key()` key path and the `ip` already returned
  by `_create_droplet` for that node.
- [ ] On verification failure:
  - [ ] `result.added` is **not** incremented for that node.
  - [ ] A descriptive message is appended to `errors` (matching the
    existing `msg = f"scale-up error for tier={tier.name}: ..."` style used
    by the adjacent `ClickException`/generic-exception branches).
  - [ ] `log.error("[autoscale] %s", msg)` is called.
  - [ ] The node is looked up via `_find_swarm_node(_client, fqdn,
    shortname)` and, if found, drained via `_drain_swarm_node`; a drain
    failure is itself caught and logged, never raised further.
  - [ ] If `_find_swarm_node` returns `None` (e.g. the join itself hadn't
    fully propagated), drain is skipped without raising — the error is
    still recorded.
  - [ ] The loop `continue`s to the next planned tier/node — it does
    **not** `break` (unlike the existing `click.ClickException`/generic
    `Exception` branches at `cs_docker/autoscale.py:1012-1022`, which
    `break` because they indicate a systemic problem, e.g. a bad DO token).
- [ ] On verification success: unchanged — `result.added += 1`,
  `log.info("[autoscale] scale-up: added node %s (tier=%s)", fqdn, tier.name)`.
- [ ] Unit tests extending `test/test_autoscale.py::TestApplyPlan` (patch
  targets **must** be `cspawn.cli.node.<name>`, matching the existing
  pattern at `test_autoscale.py:1024`, since `apply_plan` imports these
  names locally inside the function body — patching
  `cspawn.cs_docker.autoscale.<name>` would not intercept the call):
  - [ ] A plan adding two nodes where one fails verification →
    `result.added == 1`, `result.errors` contains a message referencing the
    failed node's fqdn, drain attempted only for the failed node (mock
    `_find_swarm_node`/`_drain_swarm_node` and assert call counts/args).
  - [ ] Verification failure where `_find_swarm_node` returns `None` → no
    call to `_drain_swarm_node`, no unhandled exception propagates out of
    `apply_plan`, the failure is still recorded in `result.errors`.
  - [ ] All planned nodes pass verification → `result.added` counts every
    node, `result.errors` is empty (regression guard matching today's
    pre-ticket behavior).

## Implementation Plan

**Approach**: Small, contained addition inside the existing per-tier
`try/except` loop in `apply_plan`'s scale-up section — no new control
structure, just one more step between `_join_swarm(...)` and the existing
`result.added += 1` line, plus a new failure branch that mirrors the
existing `ClickException`/generic-exception branches' logging style but
`continue`s instead of `break`s.

**Files to create/modify**:
- `cspawn/cs_docker/autoscale.py` — extend the scale-up loop in
  `apply_plan` (`cs_docker/autoscale.py:904-1022`).
- `test/test_autoscale.py` — extend `TestApplyPlan`.

**Testing plan**:
- Mock-based, per `TestApplyPlan`'s existing conventions
  (`test_autoscale.py:1008-1118`): patch `cspawn.cli.node._create_droplet`,
  `_configure_node`, `_join_swarm`, and the new `_verify_node_provisioning`
  (and `_find_swarm_node`/`_drain_swarm_node` for the drain-assertion
  tests) by their `cspawn.cli.node.*` dotted path.
- Run `uv run pytest test/test_autoscale.py -v` plus the full suite (`uv
  run pytest`) to confirm no regression to the existing `TestApplyPlan`
  dry-run and scale-down tests.

**Documentation updates**: Update `apply_plan`'s docstring (currently
describes scale-up/scale-down at a high level,
`cs_docker/autoscale.py:914-938`) to note the new post-join verification
gate and its skip-and-continue semantics on failure.

## Testing

- **Existing tests to run**: `uv run pytest test/test_autoscale.py
  test/test_node_provisioning_verify.py` (ticket 002's helpers must still
  behave identically when called from this new call site)
- **New tests to write**: extend `test/test_autoscale.py::TestApplyPlan` —
  see Acceptance Criteria and Implementation Plan above.
- **Verification command**: `uv run pytest`
