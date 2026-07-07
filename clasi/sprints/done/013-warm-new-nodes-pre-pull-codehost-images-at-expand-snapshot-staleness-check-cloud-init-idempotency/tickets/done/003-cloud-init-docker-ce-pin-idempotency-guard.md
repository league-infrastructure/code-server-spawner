---
id: '003'
title: Cloud-init docker-ce pin idempotency guard
status: done
use-cases:
- SUC-003
depends-on: []
github-issue: ''
issue: warm-new-nodes-prepull-and-snapshot-integration.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Cloud-init docker-ce pin idempotency guard

## Description

On a golden-snapshot node, docker-ce is already installed and
`apt-mark hold`ed at the manager's version as of snapshot-build time. The
sprint-012 hardened docker-ce pin block in
`config/cloud-init/swarm-node-init-v2.yaml` still unconditionally runs
`apt-get update` + the full stop/mask-contenders → install-with-retry →
`apt-mark hold` → unmask/re-enable round-trip on *every* boot — a near
no-op on a snapshot node, but a needless network/apt round trip at every
single node expand.

This ticket implements Part C2 of the issue: add a precheck that skips the
entire round-trip when `docker --version`'s major already equals the pin's
major. A missing docker (non-snapshot/base-image node) or a
present-but-wrong-major docker must still run the complete sprint-012
hardened install and fail-loud path, byte-for-byte unchanged.

This is the only part of the sprint touching cloud-init; it has no
dependency on tickets 001/002 and no Python-side change at all — the
Python side (`_resolve_cloud_init_path`, `_create_droplet`) reads this file
as an opaque blob, unaffected by what commands it contains.

## Acceptance Criteria

- [x] The four existing `runcmd` entries implementing the sprint-012
  hardened docker-ce pin (the standalone `apt-get update -qq`, the
  stop/mask-contenders entry, the install-retry-hold-assert entry, and the
  unmask/re-enable entry — currently ~lines 138-168 of
  `config/cloud-init/swarm-node-init-v2.yaml`) are consolidated into a
  single guarded `runcmd` entry.
- [x] The guard resolves `DOCKER_PIN`/`EXPECTED_MAJOR` exactly as today
  (unchanged `__DOCKER_VERSION__` substitution mechanism — no change to how
  `_create_droplet` resolves this placeholder), then precomputes
  `ACTUAL`/`ACTUAL_MAJOR` via `docker --version`, reusing the **identical**
  `grep -oE '[0-9]+' | head -n1` shell idiom the existing fail-loud
  assertion already uses (not a second, divergently-written parsing
  expression for "what is docker's major version" in shell).
- [x] **Skip branch**: when `ACTUAL_MAJOR` is non-empty and equals
  `EXPECTED_MAJOR`, logs a clear line (e.g. "docker-ce already at major N,
  matches pin — skipping install/hold round-trip") and takes no further
  action. None of `apt-get update`, the stop/mask commands, the
  `apt-get install docker-ce=...` retry loop, `apt-mark hold`, or the
  unmask/re-enable commands execute in this branch.
- [x] **Fall-through branch** (docker absent, or present with a different
  major): runs the complete, byte-for-byte-unchanged sprint-012 hardened
  sequence — `apt-get update -qq`; stop+mask
  `unattended-upgrades`/`apt-daily*`/`apt-daily-upgrade*`; install-with-retry
  (`-o DPkg::Lock::Timeout=600`, bounded retry+backoff loop); unconditional
  `apt-mark hold docker-ce docker-ce-cli`; the post-install major assertion
  (fail-loud marker file `/var/log/cspawn-docker-pin-failed` + `exit 1` on
  mismatch, preserving "no `set -e`" so the rest of `runcmd` still proceeds
  on failure); and unmask/re-enable of
  `unattended-upgrades`/`apt-daily*`/`apt-daily-upgrade*` afterward.
- [x] No change to `write_files`, the UFW configuration script
  (`configure-ufw-swarm.sh`), the do-agent install step, or the
  sshd-restart step.
- [x] `swarm-node-init-v1.yaml` remains unmodified (still confirmed
  unreachable via any deployment's `DO_CLOUD_INIT`, per sprint 012's
  finding — out of scope, unchanged from today).
- [x] Tests added/updated in `test/test_node_cloud_init.py`:
  - New test(s) asserting the precheck/skip-branch content is present in
    the rendered YAML (e.g., a comparison between `ACTUAL_MAJOR` and
    `EXPECTED_MAJOR`, and the skip-branch log line).
  - New test(s) asserting the fall-through branch's full hardened sequence
    is reachable/present (structural assertion on the guard's `else`/
    fall-through content — not a live shell execution, matching this file's
    existing content-assertion-only style; no live `apt`/`dpkg`/cloud-init
    execution anywhere in this test file).
  - Re-run and confirm the existing `TestSwarmNodeInitV2DockerPinHardening`
    class continues to pass unmodified after consolidation — all of its
    assertions operate on `_v2_runcmd_text()` (the joined string of every
    `runcmd` entry), so merging entries should not change any
    substring/index relationship it checks, but this must be verified by
    running the suite, not assumed.
- [x] Suite green: `uv run pytest --ignore=test/test_admin_coverage.py -q`.

## Implementation Plan

**Approach:** Edit only the `runcmd` list in
`config/cloud-init/swarm-node-init-v2.yaml` — merge the four existing
entries into one, wrapped in a shell `if`/`else` keyed on the
`ACTUAL_MAJOR == EXPECTED_MAJOR` precheck. Reuse the existing
major-extraction shell idiom verbatim rather than introducing a second
regex/parsing approach for "what is docker's major version" in shell —
mirroring the Python-side `_major()` single-definition principle sprint 012
already established.

**Files to create/modify:**
- `config/cloud-init/swarm-node-init-v2.yaml` — consolidate and guard the
  docker-ce pin `runcmd` block.
- `test/test_node_cloud_init.py` — new tests for both branches; confirm
  existing `TestSwarmNodeInitV2DockerPinHardening` assertions still pass.

**Documentation updates:** None required — `docs/golden-node-snapshot.md`
already documents this as "a no-op... it re-pins only if the manager has
since moved," which is the target behavior this ticket implements.

## Testing

- **Existing tests to run**: `uv run pytest --ignore=test/test_admin_coverage.py -q`
  (full suite; `test_admin_coverage.py` has known pre-existing
  PRODUCTION-env failures unrelated to this sprint — ignore). Specifically
  re-verify `test/test_node_cloud_init.py::TestSwarmNodeInitV2DockerPinHardening`
  passes unmodified after the `runcmd` consolidation.
- **New tests to write**: see the Acceptance Criteria test bullets above —
  content/structure assertions for both the skip branch (docker already
  matches) and the fall-through branch (docker missing or wrong major),
  consistent with this file's existing no-live-execution, content-assertion
  style.
- **Verification command**: `uv run pytest --ignore=test/test_admin_coverage.py -q`
