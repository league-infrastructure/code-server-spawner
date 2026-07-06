---
id: '002'
title: Race-proof and fail-loud the cloud-init docker-ce pin install
status: done
use-cases:
- SUC-002
depends-on: []
github-issue: ''
issue: node-provisioning-major-version-verify-and-race-proof-docker-install.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Race-proof and fail-loud the cloud-init docker-ce pin install

## Description

The reason `swarm5` was on docker-ce `29.6.0` (which ticket 001 stops
mis-flagging, but which shouldn't happen at all): the pin install in
`config/cloud-init/swarm-node-init-v2.yaml` (`apt-get install -y
--allow-downgrades --allow-change-held-packages "docker-ce=${DOCKER_PIN}"
"docker-ce-cli=${DOCKER_PIN}"`, `runcmd` ~lines 99-105) failed on
`/var/lib/dpkg/lock-frontend`, held by `unattended-upgrades` (confirmed
live, pid 3179). cloud-init's `runcmd` module logs a failing command's
error and continues to the next entry rather than aborting — so the
droplet reached `cloud-init status: done` while quietly still running the
base image's stock docker-ce. Nothing caught this at provisioning time;
only post-join verify (too strictly, per ticket 001) caught it minutes
later.

This ticket hardens the pin install itself, per
`clasi/issues/node-provisioning-major-version-verify-and-race-proof-docker-install.md`
and `architecture-update.md` Step 5 (M2) / Step 6:

1. **Preempt the lock contender**: stop and mask
   `unattended-upgrades.service` and the `apt-daily`/`apt-daily-upgrade`
   services/timers before the pin install runs, so none of them can
   acquire the dpkg lock during provisioning.
2. **Wait instead of fail**: run the pin `apt-get install` with `-o
   DPkg::Lock::Timeout=600` so a lock still held by something else is
   waited out, not immediately fatal.
3. **Retry**: wrap the install in a bounded retry-with-backoff loop to
   absorb any transient apt failure that survives the lock-timeout.
4. **Hold, unconditionally** (existing step, preserved): `apt-mark hold
   docker-ce docker-ce-cli` already runs today immediately after the
   install attempt. This ticket keeps it, but now runs it regardless of
   whether the pin install actually converged on the intended version —
   whatever docker-ce/docker-ce-cli build ends up installed gets held
   against further automatic-upgrade drift.
5. **Fail loud**: after the hold, assert that the installed docker-ce
   major matches `DOCKER_PIN`'s major; on mismatch, write a clear,
   greppable marker and exit non-zero for that step, so `cloud-init
   status` itself reflects the failure instead of reporting `done` while
   quietly wrong.
6. **Re-enable normal patching** (stakeholder decision, 2026-07-06):
   unmask and re-enable `unattended-upgrades.service` and the
   `apt-daily`/`apt-daily-upgrade` timers **after** the hold is set, so
   nodes keep receiving OS security patches for their lifetime rather than
   staying permanently masked. This is safe specifically *because* step 4
   already holds `docker-ce`/`docker-ce-cli` at this point — held packages
   are skipped by `unattended-upgrades` and by `apt-get
   upgrade`/`dist-upgrade` invoked from the `apt-daily-upgrade` path — so
   resuming automatic OS patching cannot silently drift the swarm-critical
   docker-ce version out from under a running node. Re-enabling happens
   unconditionally (whether or not the version assertion in step 5
   passed), since the hold — not the timers being masked — is what
   protects docker-ce from drift; a node with a still-wrong version is
   caught by the fail-loud marker/exit and by `_verify_node_provisioning`,
   not by leaving its OS patching permanently disabled.

`swarm-node-init-v1.yaml` is confirmed **not** selectable in any current
deployment (`DO_CLOUD_INIT=swarm-node-init-v2.yaml` in
`config/{devel,local-prod,prod}/public.env`) and contains no docker-ce
install step at all — left unmodified, per the issue's conditional scope.

## Acceptance Criteria

- [x] `swarm-node-init-v2.yaml`'s `runcmd` stops and masks
  `unattended-upgrades.service`, `apt-daily.service`/`apt-daily.timer`, and
  `apt-daily-upgrade.service`/`apt-daily-upgrade.timer` before the existing
  docker-ce pin install step.
- [x] The pin `apt-get install` invocation adds `-o
  DPkg::Lock::Timeout=600` (dpkg/apt's own lock-wait; no hand-rolled
  polling loop required for this part).
- [x] The pin install is wrapped in a bounded retry-with-backoff (a small,
  fixed number of attempts, e.g. 3-5, with an increasing sleep between
  them) to absorb a transient apt failure that survives the lock-timeout.
- [x] The existing `apt-mark hold docker-ce docker-ce-cli` step is
  preserved and runs unconditionally immediately after the install
  attempt(s) — regardless of whether the install actually converged on
  the pinned version.
- [x] After the hold, a new step asserts the installed docker-ce major
  matches `DOCKER_PIN`'s major (parsed the same "leading integer" way
  `_major()` does on the Python side, expressed in shell since this runs
  inside cloud-init):
  - [x] On match: no visible effect (provisioning continues normally).
  - [x] On mismatch: writes a clear, greppable marker (e.g.
    `/var/log/cspawn-docker-pin-failed`, containing the expected and
    actual versions) and logs an unambiguous `ERROR`-labeled line to
    cloud-init's own output, and the step itself exits non-zero so
    `cloud-init status` reports an error for this stage — not `status:
    done`.
  - [x] This step does not abort the remainder of `runcmd` (do-agent
    install, UFW configuration, sshd tuning) — cloud-init's existing
    per-command continue-on-error behavior for `runcmd` is deliberately
    preserved so a node with a failed docker pin is still otherwise
    reachable/inspectable/fixable by an operator, exactly as today for
    every *other* `runcmd` entry.
- [x] `unattended-upgrades.service` and the `apt-daily`/`apt-daily-upgrade`
  services/timers are unmasked and re-enabled (restored to their normal
  running/enabled state) after the hold step, unconditionally (regardless
  of the version-assertion outcome).
- [x] `swarm-node-init-v1.yaml` is unmodified; a comment or this ticket's
  record (not a code change) confirms it is unreachable via
  `DO_CLOUD_INIT`/`DO_CLOUD_INIT_FILE` in all three `public.env` files.
- [x] `test/test_node_cloud_init.py` gains content assertions on the
  rendered `swarm-node-init-v2.yaml` (reading the real file, following
  this file's existing pattern of asserting on YAML/shell content rather
  than executing real apt/dpkg):
  - [x] Asserts the stop+mask step names `unattended-upgrades`,
    `apt-daily`, and `apt-daily-upgrade` units.
  - [x] Asserts the pin install command contains `Lock::Timeout` (or the
    chosen equivalent) and is wrapped in a retry construct (e.g. asserts
    a loop keyword and more than one attempt).
  - [x] Asserts `apt-mark hold docker-ce docker-ce-cli` is still present.
  - [x] Asserts a fail-loud marker path/string and a non-zero-exit
    construct are present in a step that runs after the hold.
  - [x] Asserts an unmask/re-enable step naming the same units as the
    stop+mask step is present, after the fail-loud assertion step.
- [x] Full suite green: `uv run pytest` (excluding the known pre-existing
  `test_admin_coverage.py` PRODUCTION-env failures).

## Implementation Plan

**Approach**: Edit only `config/cloud-init/swarm-node-init-v2.yaml`'s
`runcmd` section (no `write_files`/UFW changes). Insert the lock-preemption
step before the existing `apt-get update -qq`; modify the existing pin
install entry to add the lock-timeout option and a retry wrapper; keep
`apt-mark hold docker-ce docker-ce-cli` in place; add a new fail-loud
assertion entry immediately after the hold; add a new unmask/re-enable
entry after the assertion; leave the rest of `runcmd` (do-agent install,
UFW config, sshd restart) untouched. Illustrative sketch (exact
quoting/wording left to implementation):

```yaml
runcmd:
  - apt-get update -qq
  - >-
    systemctl stop unattended-upgrades.service apt-daily.service
    apt-daily.timer apt-daily-upgrade.service apt-daily-upgrade.timer || true;
    systemctl mask unattended-upgrades.service apt-daily.service
    apt-daily.timer apt-daily-upgrade.service apt-daily-upgrade.timer || true
  - >-
    . /etc/os-release;
    DOCKER_PIN="5:29.6.1-1~ubuntu.${VERSION_ID}~${VERSION_CODENAME}";
    ok=0;
    for attempt in 1 2 3 4 5; do
      if apt-get install -y --allow-downgrades --allow-change-held-packages
        -o DPkg::Lock::Timeout=600
        "docker-ce=${DOCKER_PIN}" "docker-ce-cli=${DOCKER_PIN}"; then
        ok=1; break;
      fi;
      echo "docker-ce pin install attempt ${attempt} failed; retrying" >&2;
      sleep $((attempt * 10));
    done
  - apt-mark hold docker-ce docker-ce-cli
  - >-
    EXPECTED_MAJOR="29";
    ACTUAL="$(docker --version 2>/dev/null || true)";
    ACTUAL_MAJOR="$(echo "$ACTUAL" | grep -oE '[0-9]+' | head -n1)";
    if [ "$ACTUAL_MAJOR" != "$EXPECTED_MAJOR" ]; then
      echo "CSPAWN ERROR: docker-ce pin failed - expected major ${EXPECTED_MAJOR}, got '${ACTUAL}'" >&2;
      printf 'expected_major=%s actual=%s\n' "$EXPECTED_MAJOR" "$ACTUAL" > /var/log/cspawn-docker-pin-failed;
      exit 1;
    fi
  - >-
    systemctl unmask unattended-upgrades.service apt-daily.service
    apt-daily.timer apt-daily-upgrade.service apt-daily-upgrade.timer || true;
    systemctl enable --now apt-daily.timer apt-daily-upgrade.timer || true
  - systemctl enable --now docker
```

Key implementation caveat to carry forward: cloud-init's `runcmd` module
does **not** abort later entries when one entry exits non-zero (this
continue-on-error behavior is what made the original bug silent, and is
deliberately *kept* here, not changed — see Acceptance Criteria). The
`exit 1` in the fail-loud step therefore doesn't halt provisioning; it
changes what `cloud-init status` reports for that stage from implicitly-ok
to explicitly-errored, which is the actual signal both a human reading
`cloud-init status`/`/var/log/cloud-init-output.log` and
`_verify_node_provisioning`'s existing check (c) (`"status: done"`) already
consume. `DOCKER_PIN_MAJOR`/`EXPECTED_MAJOR` should be derived from the
same `DOCKER_PIN=` value already declared earlier in the block (avoid a
second hardcoded `"29"` literal drifting from the real pin) — the sketch
above hardcodes it only for illustration.

**Files to create/modify**:
- `config/cloud-init/swarm-node-init-v2.yaml` — insert/modify the
  `runcmd` steps as described above.
- `test/test_node_cloud_init.py` — add content-assertion tests on the
  rendered v2 YAML (read the real file from `config/cloud-init/`, per this
  file's existing project-fixture convention).

**Testing plan**:
- Read `config/cloud-init/swarm-node-init-v2.yaml`'s actual content in the
  new tests and assert the presence of: the stop+mask unit names, the
  `Lock::Timeout` (or equivalent) option, a retry-loop construct, the
  preserved `apt-mark hold docker-ce docker-ce-cli` line, a fail-loud
  marker path and non-zero-exit construct positioned after the hold, and
  an unmask/re-enable step naming the same units, positioned after the
  fail-loud step.
- Run `uv run pytest test/test_node_cloud_init.py -v`, then the full
  suite.
- Real dpkg-lock-contention behavior (does the lock-timeout/retry actually
  survive a live `unattended-upgrades` race) is not exercisable in a unit
  test and is explicitly out of this ticket's automated-test scope — this
  sprint ships code + tests only, no deploy; a manual smoke test after the
  next image build/deploy is a follow-up operational step, not part of
  this ticket.

**Documentation updates**: A short comment block above the new `runcmd`
steps explaining the stop/mask → install (lock-wait + retry) → hold →
assert → re-enable ordering, and explicitly noting that re-enabling
`unattended-upgrades`/`apt-daily*` afterward is safe because the hold (not
the masking) is what protects `docker-ce`/`docker-ce-cli` from automatic-
upgrade drift.

## Testing

- **Existing tests to run**: `uv run pytest test/test_node_cloud_init.py
  test/test_node_provisioning_verify.py` (the latter's `_expected_docker_version`
  tests parse the same `DOCKER_PIN=` line this ticket's `runcmd` changes
  surround, but do not touch — confirm no regression).
- **New tests to write**: content assertions on the hardened
  `swarm-node-init-v2.yaml`, as detailed in Acceptance Criteria and the
  Implementation Plan.
- **Verification command**: `uv run pytest`
