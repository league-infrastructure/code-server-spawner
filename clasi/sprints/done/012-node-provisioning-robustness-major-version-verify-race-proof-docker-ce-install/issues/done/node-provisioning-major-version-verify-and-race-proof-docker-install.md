---
status: done
sprint: '012'
tickets:
- 012-001
- 012-002
---

# Node provisioning: verify docker by MAJOR version, and race-proof the cloud-init docker-ce install

## Summary

Two related node-provisioning defects, both surfaced live on 2026-07-06 when a
`swarm5` expand was drained mid-class and left the fleet short a node (which
then cascaded into codehost overload + student disconnects):

1. **Post-join verify is too strict (exact version).**
   `_verify_node_provisioning` ([node.py:585-654](cspawn/cli/node.py#L585),
   check at ~628-638) fails the node unless the *exact* expected version
   string (e.g. `29.6.1`) is a substring of `docker --version`. A worker on
   `29.6.0` — which Docker Swarm runs fine alongside `29.6.1` managers (swarm
   join only requires **major**-version compatibility) — is drained. That is
   exactly what happened to swarm5 (`29.6.0` vs manager `29.6.1`).

2. **The cloud-init docker-ce pin install races `unattended-upgrades`.**
   The reason swarm5 was on `29.6.0` at all: the pin install in
   [swarm-node-init-v2.yaml:100-104](config/cloud-init/swarm-node-init-v2.yaml#L100)
   (`apt-get install docker-ce=${DOCKER_PIN} ...`) **failed on a dpkg lock** —
   `unattended-upgrades` (pid 3179) held `/var/lib/dpkg/lock-frontend`.
   cloud-init logged the error and continued, leaving docker at the base
   image's `29.6.0`. The pin target (`29.6.1`) was correct and available in
   the apt repo (`apt-cache madison docker-ce` lists it); the install simply
   never ran. The failure was silent until post-join verify caught it.

## Fix

### Part A — verify by major version (stakeholder-selected)
- Change `_verify_node_provisioning`'s docker-version check to compare **major
  version only** (worker major == expected major), instead of exact-substring.
  Reuse the existing `_major(...)` helper already used by the join preflight
  ([node.py:1517-1524](cspawn/cli/node.py#L1517)) so join-preflight and
  post-join-verify use identical version logic.
- `_expected_docker_version` still yields the pinned `X.Y.Z`; the verify just
  reduces both sides to major. Keep `None` → skip (unchanged).
- Keep the check as a hard failure only on a genuine **major** mismatch; a
  patch/minor difference must PASS (so a transient `29.6.0` vs `29.6.1` node is
  no longer drained).

### Part B — race-proof + fail-loud the docker-ce install
In `config/cloud-init/swarm-node-init-v2.yaml`, before/around the docker-ce pin:
- **Preempt the lock contender:** stop and mask `unattended-upgrades` (and the
  `apt-daily`/`apt-daily-upgrade` timers) for the duration of provisioning so
  they don't grab the dpkg lock mid-install.
- **Wait for the lock instead of failing:** run the pin `apt-get install` with
  `-o DPkg::Lock::Timeout=600` (or an explicit fuser/flock wait loop on
  `/var/lib/dpkg/lock-frontend`) so a busy lock is waited out, not fatal.
- **Retry** the pin install a few times with backoff to absorb transient apt
  failures.
- **Fail loud:** after the install, assert `docker --version`'s major (or the
  pinned version) actually matches the intended pin; if not, make cloud-init
  surface a clear error (non-zero / explicit error marker) so the node's bad
  state is obvious at provision time, not only at post-join verify.
- Apply the same treatment to `swarm-node-init-v1.yaml` only if it is still a
  supported/selectable cloud-init (`DO_CLOUD_INIT` is currently
  `swarm-node-init-v2.yaml`); otherwise leave v1 alone and note it.

## Out of scope
- Dynamic "install exactly the manager's version" pin (querying the manager at
  expand and templating the cloud-init). Discussed but deferred — with Part A
  (major-version verify) + Part B (robust install), the existing hardcoded pin
  is no longer fragile enough to require it this sprint. Capture as a follow-up
  if desired.
- Pinning codehosts to their node so Swarm never migrates them (separate
  host-placement concern; separate future sprint).
- Any change to the node capacity/rebalance logic.

## Acceptance criteria (draft)
- [ ] `_verify_node_provisioning` passes a node whose docker major matches the
  expected major but whose patch differs (e.g. expected `29.6.1`, node
  `29.6.0`); still fails on a real major mismatch (e.g. `28.x`). Unit test with
  mocked `_ssh_exec` output covering both.
- [ ] Join-preflight and post-join-verify use the same `_major`-based logic (no
  duplicated/inconsistent parsing).
- [ ] cloud-init stops/masks `unattended-upgrades` before the docker-ce pin and
  installs with a dpkg-lock wait + retry; a lock held at install time no longer
  leaves docker unpinned.
- [ ] cloud-init fails loudly (clear error, non-zero/marker) if the pinned
  docker version did not actually get installed.
- [ ] Existing node tests updated (`test/test_node_provisioning_verify.py`,
  `test/test_node_cloud_init.py`); suite green (excluding the known
  pre-existing `test_admin_coverage.py` PRODUCTION-env failures).

## Notes
- Root incident 2026-07-06: swarm3 removed + swarm5 drained (this bug) → 21
  codehosts crammed onto swarm2(6)+swarm4(14) → swarm2 overload (load 12.9) →
  Swarm rescheduled its tasks → fleet-wide student disconnects. Restoring
  reliable node adds (this issue) is what prevents the capacity shortfall that
  triggered the cascade.
