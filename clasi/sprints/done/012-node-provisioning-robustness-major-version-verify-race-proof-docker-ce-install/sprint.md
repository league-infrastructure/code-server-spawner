---
id: '012'
title: "Node provisioning robustness \u2014 major-version verify + race-proof docker-ce\
  \ install"
status: done
branch: sprint/012-node-provisioning-robustness-major-version-verify-race-proof-docker-ce-install
use-cases: []
issues:
- node-provisioning-major-version-verify-and-race-proof-docker-install.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 012: Node provisioning robustness — major-version verify + race-proof docker-ce install

## Goals

Restore reliable, unattended node provisioning after the 2026-07-06 `swarm5`
incident, by fixing the two compounding defects that caused it: an
over-strict post-join docker-version check, and a cloud-init docker-ce pin
install that can silently no-op under a dpkg-lock race.

## Problem

A `swarm5` node expand was drained mid-class over a docker **patch**
mismatch (`29.6.0` vs manager `29.6.1`) — a difference Docker Swarm doesn't
care about (join/operation only require major-version compatibility, the
same standard `_join_swarm`'s own pre-join preflight already applies).
Draining swarm5 left the fleet a node short, overpacking codehosts onto
swarm2+swarm4, overloading swarm2 (load 12.9), triggering Swarm task
rescheduling and fleet-wide student disconnects.

The reason swarm5 was on `29.6.0` in the first place: the docker-ce pin
install in `config/cloud-init/swarm-node-init-v2.yaml` failed on a dpkg
lock held by `unattended-upgrades`; cloud-init logged the error and
continued, leaving docker at the base image's version. This failure was
silent until post-join verify caught it (too strictly) minutes later.

## Solution

Two independent fixes, both confirmed via live diagnosis and detailed in
`architecture-update.md`:

- **Part A** — Change `_verify_node_provisioning`'s docker-version check
  (`cspawn/cli/node.py:585-654`) to compare **major version only**, reusing
  the existing `_major()` helper currently private to `_join_swarm`'s
  pre-join preflight (promoted to module scope so both gates share one
  parsing definition). A patch/minor difference now passes; a real major
  mismatch still fails.
- **Part B** — Harden the docker-ce pin install in
  `config/cloud-init/swarm-node-init-v2.yaml`: stop/mask
  `unattended-upgrades` and the `apt-daily*` timers before the pin install,
  run the install with a dpkg lock-timeout and bounded retry/backoff, and
  assert post-install that the installed major matches the pin — failing
  loudly (marker + non-zero) instead of silently continuing on mismatch.
  `swarm-node-init-v1.yaml` is confirmed unreachable in all three deployment
  configs (`DO_CLOUD_INIT=swarm-node-init-v2.yaml` everywhere) and is left
  unmodified.

## Success Criteria

- A node whose docker-ce major matches the pin's major (but whose
  patch/minor differs) passes post-join verification; a genuine major
  mismatch still fails it.
- The join preflight and post-join verify share one `_major()` definition —
  no duplicated/inconsistent version parsing.
- The cloud-init pin install survives a concurrent dpkg lock (stop/mask the
  known contenders, wait-then-retry on the lock) and fails loudly, with a
  greppable marker, if the pin still doesn't take.
- Full test suite green (`test/test_node_provisioning_verify.py`,
  `test/test_node_cloud_init.py`, and the rest), excluding the known
  pre-existing `test_admin_coverage.py` PRODUCTION-env failures.
- No deploy this sprint — code + tests merged to `master` only; ships with
  the next spawner image build, at the stakeholder's discretion.

## Scope

### In Scope

- `_verify_node_provisioning`'s docker-version comparison logic
  (`cspawn/cli/node.py`).
- Promoting `_major()` to module scope and reusing it from both
  `_join_swarm`'s preflight and `_verify_node_provisioning`.
- Hardening the docker-ce pin install in
  `config/cloud-init/swarm-node-init-v2.yaml` (lock preemption, lock-wait,
  retry, fail-loud assertion).
- Confirming (not necessarily changing) whether
  `config/cloud-init/swarm-node-init-v1.yaml` needs the same treatment.
- Test updates/additions in `test/test_node_provisioning_verify.py` and
  `test/test_node_cloud_init.py`.

### Out of Scope

- Dynamic "pin to the manager's exact live version" templating at expand
  time (deferred — this sprint's two fixes make the current hardcoded pin
  adequately robust; capture as a follow-up if still desired).
- Pinning codehosts to their node so Swarm never migrates them (separate
  host-placement concern, separate future sprint).
- Any change to node capacity/rebalance/scheduling logic.
- Actual deployment of the fix (stakeholder is holding deploy until after
  class).

## Test Strategy

Unit tests only, no live infrastructure:

- `test/test_node_provisioning_verify.py`: update the existing
  exact-substring mismatch/match assertions to reflect major-only
  comparison; add a case for "major matches, patch differs → passes" and
  keep "major differs → fails"; verify `_major()` is now importable at
  module scope and used identically by both call sites (directly or via a
  shared-behavior assertion).
- `test/test_node_cloud_init.py`: add content assertions on the rendered
  `swarm-node-init-v2.yaml` — presence of the unattended-upgrades/apt-daily
  stop+mask steps, the lock-timeout/retry-wrapped install, and the
  fail-loud post-install assertion/marker. Follows this file's existing
  convention of asserting YAML/shell content rather than executing real
  apt/dpkg.
- Full suite: `uv run pytest`, excluding the known pre-existing
  `test_admin_coverage.py` PRODUCTION-env failures.

## Architecture Notes

See `architecture-update.md` for full detail. Key points: the two fixes
(M1: version-compatibility logic in `cli/node.py`; M2: install robustness in
`swarm-node-init-v2.yaml`) are structurally independent — neither depends on
the other, which is why their tickets carry no `depends-on` relationship.
M1 is a coupling *reduction* (collapses two independent major-parsing paths
into one shared function). M2 introduces no new codebase dependency at all
(pure cloud-init YAML/shell content change). **Resolved 2026-07-06**:
`unattended-upgrades`/`apt-daily*` are unmasked and re-enabled after the
docker-ce pin install completes (nodes keep getting OS security patches),
and the existing `apt-mark hold docker-ce docker-ce-cli` step is preserved
and now runs unconditionally right after the install — the hold, not the
masking, is what makes re-enabling automatic upgrades safe (held packages
are skipped by `unattended-upgrades`/`apt-daily-upgrade`). Baked into
ticket 002.

## GitHub Issues

(No GitHub issues linked yet; tracked via
`clasi/issues/node-provisioning-major-version-verify-and-race-proof-docker-install.md`.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [ ] Architecture review passed
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Post-join verify compares docker version by major only, reusing _major | — |
| 002 | Race-proof and fail-loud the cloud-init docker-ce pin install | — |

Tickets execute serially in the order listed, but carry no `depends-on`
relationship — Part A (version-compatibility check) and Part B (cloud-init
install hardening) are structurally independent fixes for two separate
defects; either can be implemented first.
