---
id: '002'
title: Snapshot staleness WARNING on docker major mismatch
status: done
use-cases:
- SUC-002
depends-on: []
github-issue: ''
issue: warm-new-nodes-prepull-and-snapshot-integration.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Snapshot staleness WARNING on docker major mismatch

## Description

The golden DO snapshot's baked docker-ce version (id `235956540`, docker
29.6.1, held) is frozen at snapshot-build time and can silently drift from
the swarm manager's as the manager is upgraded over months. The existing
post-join `_verify_node_provisioning` (sprint 009/012) already hard-fails a
real major mismatch and drains the node — but its failure message
("docker version mismatch: expected X, got Y") doesn't tell an operator
*why* a node that should have been correctly baked ended up wrong, or what
to do about it.

This ticket implements Part C1 of the issue: at the same post-join point,
independently compare the node's docker-ce major against the manager's live
major, and when they differ, log a WARNING naming the likely cause
("provisioning from a golden snapshot whose baked docker-ce has drifted
from the manager's current version") and the concrete remedy
(`scripts/build-golden-node-snapshot.sh`, see `docs/golden-node-snapshot.md`).

This is **purely diagnostic** — it complements, and must never replace or
alter, `_verify_node_provisioning`'s existing hard-gate pass/fail verdict,
failure message, or drain-and-abort/drain-and-continue behavior. It must be
wired into both `expand()` (`cspawn/cli/node.py`) and `apply_plan()`'s
scale-up loop (`cspawn/cs_docker/autoscale.py`), since (per
architecture-update.md's Step 1 finding) `apply_plan()` does not call
`expand()` — it duplicates the create/configure/join/verify sequence
inline.

## Acceptance Criteria

- [x] New `_check_docker_staleness(ip: str, key_path: Path, *,
  expected_docker_version: str | None, log=None) -> None` helper added to
  `cspawn/cli/node.py`, near `_verify_node_provisioning`/`_major`. Skips
  entirely (no-op, no log line) when `expected_docker_version is None` —
  matching `_verify_node_provisioning`'s own skip condition for "nothing to
  compare against."
- [x] Otherwise runs `docker --version` over SSH (best-effort — an SSH
  failure here is treated as "can't compare" and is *not* itself escalated
  to WARNING, since `_verify_node_provisioning`'s own SSH-reachability check
  already surfaces node unreachability), computes both majors via the
  existing shared `_major()` (no second/divergent parsing definition), and
  when both majors are resolvable and differ, logs a WARNING naming golden
  snapshot staleness as the likely cause and
  `scripts/build-golden-node-snapshot.sh` as the remedy. Never raises.
- [x] Wired into `expand()`: called once, after `_verify_node_provisioning`
  runs, passing the same `expected_docker_version` value already computed
  for that verify call (`_manager_docker_version(manager_client) or
  _expected_docker_version(cfg)`) — no second, independent resolution of
  the expected version.
- [x] Wired into `apply_plan()`'s scale-up loop identically, per node,
  reusing the same `expected_docker_version` value already computed for
  that loop's `_verify_node_provisioning` call.
- [x] The WARNING never alters `_verify_node_provisioning`'s pass/fail
  verdict, its existing failure message text, or the existing
  drain-and-abort (`expand()`)/drain-and-continue (`apply_plan()`) behavior
  on a real verify failure — purely additive diagnostic logging alongside
  the unchanged hard gate.
- [x] No WARNING is logged when the majors match (including when a node is
  correctly provisioned from a golden snapshot that still matches the
  manager).
- [x] Tests added/updated:
  - `test/test_node_provisioning_verify.py`: new test class (e.g.
    `TestExpandDockerStalenessWarning`) covering: WARNING fires and names
    both "golden snapshot" staleness and the rebuild script when majors
    differ; no WARNING when majors match; no WARNING/no crash when
    `expected_docker_version` is `None`; the check does not affect
    `expand()`'s exit code or the existing verify-failure/-success test
    assertions in `TestExpandVerificationFailure`/`TestExpandVerificationSuccess`.
  - `test/test_autoscale.py`: equivalent coverage for `apply_plan`'s
    scale-up loop, patched at `cspawn.cli.node._check_docker_staleness` per
    this file's documented convention.
- [x] Suite green: `uv run pytest --ignore=test/test_admin_coverage.py -q`.

## Implementation Plan

**Approach:** Add `_check_docker_staleness` as a new, small, single-purpose
module-level function reusing `_ssh_exec`/`_major` — deliberately kept
separate from `_verify_node_provisioning` (whose contract and extensive
existing test coverage stay untouched) rather than threading extra return
data through it (see architecture-update.md Design Rationale for why this
was chosen over refactoring `_verify_node_provisioning`'s return type). Call
it from `expand()` and `apply_plan()` immediately alongside their existing
`_verify_node_provisioning` calls, reusing the already-computed
`expected_docker_version` value at each call site.

**Files to create/modify:**
- `cspawn/cli/node.py` — new `_check_docker_staleness` helper, `expand()`
  wiring.
- `cspawn/cs_docker/autoscale.py` — `apply_plan()` scale-up loop wiring, new
  import added to the existing lazy `from cspawn.cli.node import (...)`
  block.
- `test/test_node_provisioning_verify.py` — new test class.
- `test/test_autoscale.py` — new/extended test coverage.

**Documentation updates:** None required — `docs/golden-node-snapshot.md`
already documents this staleness/rebuild guidance as the target behavior.

## Testing

- **Existing tests to run**: `uv run pytest --ignore=test/test_admin_coverage.py -q`
  (full suite; `test_admin_coverage.py` has known pre-existing
  PRODUCTION-env failures unrelated to this sprint — ignore). In particular,
  confirm `TestExpandVerificationFailure`/`TestExpandVerificationSuccess`
  and `TestApplyPlanScaleUpVerification` still pass unmodified — this
  ticket must not change verify's pass/fail behavior.
- **New tests to write**: see the Acceptance Criteria test bullets above —
  WARNING fires only on a resolvable major mismatch, names both cause and
  remedy, never fires on a match, never raises, and never affects the
  existing verify verdict — for both `expand()` and `apply_plan()`.
- **Verification command**: `uv run pytest --ignore=test/test_admin_coverage.py -q`
