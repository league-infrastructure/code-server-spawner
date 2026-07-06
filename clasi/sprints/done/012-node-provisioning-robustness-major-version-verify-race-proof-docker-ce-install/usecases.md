---
status: approved
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 012 Use Cases

## SUC-001: Post-Join Verification Accepts a Node Whose Docker Patch Differs From the Pin
Parent: UC-006 (Operator Adds a Swarm Node via CLI)

- **Actor**: Admin / Operator (`cspawnctl node expand`); the autoscaler
  control loop (`cspawn/cs_docker/autoscale.py::apply_plan`,
  cron-triggered scale-up).
- **Preconditions**: A node has just joined the swarm (via `_create_droplet`
  → `_configure_node` → `_join_swarm`). Its cloud-init docker-ce pin install
  completed, but the installed version's **patch** number differs from the
  pinned `expected_docker_version` returned by `_expected_docker_version`
  (e.g. node reports `29.6.0`, cloud-init pins `29.6.1`) while the **major**
  version matches (`29` == `29`). This is exactly what happened to `swarm5`
  on 2026-07-06: Docker Swarm joins and operates worker nodes fine across a
  patch/minor difference — only a major-version mismatch risks a broken
  swarm TLS/API handshake — but `_verify_node_provisioning`'s check today
  requires the *entire* pinned string to be a substring of `docker
  --version`'s output, so a real, harmless patch difference fails the node
  anyway.
- **Main Flow**:
  1. Immediately after the node is confirmed present in swarm membership,
     the caller (`expand()` or `apply_plan()`) runs
     `_verify_node_provisioning`.
  2. The docker-version check parses the major version out of both
     `expected_docker_version` (from `_expected_docker_version`) and the
     node's own `docker --version` output, using the same `_major(...)`
     parsing logic already used by `_join_swarm`'s pre-join preflight
     (`cli/node.py:1566-1573`).
  3. The two majors match (`29` == `29`) even though the full version
     strings differ (`29.6.1` vs `29.6.0`).
  4. The check passes; the node is not flagged, not drained, and remains
     eligible for scheduling.
- **Postconditions**: A node running a patch/minor-different but
  major-compatible docker-ce build is treated as healthy. A node whose
  **major** version genuinely differs from the pin (e.g. `28.x` vs a `29.x`
  pin) still fails this check, exactly as before.
- **Error Flows**:
  - `expected_docker_version` is `None` (no cloud-init configured / pattern
    not found): the version check is skipped entirely — unchanged behavior.
  - Node's `docker --version` is unparseable (empty output, SSH failure):
    major resolves to `None`; treated as a failure (cannot confirm
    compatibility), same conservative posture as today's substring check
    failing on empty output.
- **Acceptance Criteria**:
  - [ ] A node whose docker major matches the expected major but whose
    patch/minor differs (expected `29.6.1`, node reports `29.6.0`) passes
    `_verify_node_provisioning`'s version check.
  - [ ] A node whose docker major genuinely differs (expected `29.6.1`,
    node reports `28.4.0`) still fails the check, with a failure string
    naming expected vs. actual.
  - [ ] `_verify_node_provisioning`'s version check and `_join_swarm`'s
    pre-join preflight both resolve "major version" via the same
    module-level `_major(...)` helper — no second, independently
    maintained parsing regex.
  - [ ] `expected_docker_version=None` still skips the check entirely
    (regression guard, unchanged from today).

## SUC-002: Docker-CE Pin Install Survives a Concurrent `unattended-upgrades` dpkg Lock
Parent: UC-006 (Operator Adds a Swarm Node via CLI)

- **Actor**: cloud-init, running unattended on a freshly created droplet
  during first boot (no human actor — this is the provisioning path that
  produces the node `_join_swarm`/`_verify_node_provisioning` later inspect).
- **Preconditions**: A droplet boots from the DigitalOcean base image with
  `swarm-node-init-v2.yaml` as its `user_data`. The base image's
  `unattended-upgrades` service (and/or the `apt-daily`/`apt-daily-upgrade`
  systemd timers) may independently acquire `/var/lib/dpkg/lock-frontend` at
  any point during first boot — exactly what happened on `swarm5`
  2026-07-06 (`unattended-upgrades` pid 3179 held the lock when the pin
  install ran).
- **Main Flow**:
  1. Before the docker-ce pin install runs, cloud-init stops and masks
     `unattended-upgrades.service` and the `apt-daily.timer`/
     `apt-daily-upgrade.timer` units, so none of them can acquire the dpkg
     lock during provisioning.
  2. cloud-init runs `apt-get install docker-ce=${DOCKER_PIN}
     docker-ce-cli=${DOCKER_PIN}` with `-o DPkg::Lock::Timeout=600` (waiting
     out a lock instead of failing immediately), wrapped in a retry-with-
     backoff loop to absorb transient apt failures.
  3. The install succeeds — either immediately (no contender) or after
     waiting out / retrying past a transient lock holder.
  4. cloud-init asserts, by running `docker --version` locally, that the
     installed version's major (at minimum) matches the pin. The assertion
     passes.
  5. cloud-init proceeds to `apt-mark hold` and the rest of the `runcmd`
     sequence unchanged.
- **Postconditions**: The node's docker-ce is at the pinned version (or, if
  step 2 could never win the lock after retries, the failure is surfaced
  loudly at provisioning time rather than silently leaving the base image's
  version in place). A node that reaches `_join_swarm`/`_verify_node_provisioning`
  never silently carries an unpinned docker-ce version whose cause was a
  dpkg-lock race, because that race can no longer occur unnoticed.
- **Error Flows**:
  - The pin install still fails after all retries/lock-wait budget is
    exhausted (e.g. a genuinely broken apt repo, not just a transient
    lock): cloud-init's post-install version assertion fails and writes a
    clear, greppable error/marker (e.g. to the cloud-init log and a marker
    file) instead of logging-and-continuing — so the bad state is visible
    at provision time, not only later at post-join verify.
  - `swarm-node-init-v1.yaml` is not selectable in any current deployment
    (`DO_CLOUD_INIT=swarm-node-init-v2.yaml` in every `public.env`); it
    carries no docker-ce install at all, so this hardening does not apply
    to it — noted, not changed, per the issue's explicit conditional scope.
- **Acceptance Criteria**:
  - [ ] `swarm-node-init-v2.yaml` stops and masks `unattended-upgrades`
    (and the `apt-daily`/`apt-daily-upgrade` timers) before the docker-ce
    pin install step.
  - [ ] The pin `apt-get install` runs with `-o DPkg::Lock::Timeout=600`
    (or an equivalent explicit wait on `/var/lib/dpkg/lock-frontend`) so a
    lock held at install time is waited out, not immediately fatal.
  - [ ] The pin install is retried a bounded number of times with backoff
    before being treated as failed.
  - [ ] After the install (and retries), cloud-init asserts the installed
    docker-ce version's major matches the pin's major; a mismatch produces
    a clear, non-silent error/marker (distinct from today's "log and
    continue").
  - [ ] `swarm-node-init-v1.yaml` is confirmed not selectable via
    `DO_CLOUD_INIT`/`DO_CLOUD_INIT_FILE` in any of `config/{devel,local-prod,
    prod}/public.env`; left unmodified, with that finding recorded in the
    architecture update rather than silently skipped.
  - [ ] Test coverage (`test/test_node_cloud_init.py` or a sibling test)
    asserts the rendered `swarm-node-init-v2.yaml` content contains the
    unattended-upgrades stop/mask step, the `Lock::Timeout`/lock-wait
    option on the pin install, and the fail-loud marker/assertion step.
