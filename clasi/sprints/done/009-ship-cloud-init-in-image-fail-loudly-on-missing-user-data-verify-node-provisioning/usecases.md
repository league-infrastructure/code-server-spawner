---
status: draft
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 009 Use Cases

## SUC-001: Operator Provisions a Node from Inside the Spawner Container
Parent: UC-006 (Operator Adds a Swarm Node via CLI)

- **Actor**: Admin / Operator (running `cspawnctl node expand` from a shell
  inside the deployed spawner container, or triggering it indirectly via the
  admin UI's "Synchronize" action).
- **Preconditions**: `DO_TOKEN`, `DO_NAMES`, `DOCKER_URI` configured;
  `DO_CLOUD_INIT=swarm-node-init-v2.yaml` configured (as it is in every
  deployment's `public.env` today).
- **Main Flow**:
  1. Operator runs `cspawnctl node expand --create` (or the full `expand`
     flow) from inside the container.
  2. `_create_droplet` resolves the configured cloud-init file at
     `/app/config/cloud-init/swarm-node-init-v2.yaml`.
  3. The file exists (shipped in the image by this sprint) — its contents
     are read and passed as `user_data` to the DigitalOcean droplet-create
     call.
  4. DigitalOcean creates the droplet with cloud-init `user_data` attached;
     on first boot, cloud-init pins docker-ce, fixes the UFW `22/tcp` rule,
     applies swarm-dataplane firewall rules, and tunes sshd.
- **Postconditions**: The new droplet boots with the same provisioning it
  would get from a full local checkout — no behavioral difference between
  "expand run in-container" and "expand run from a full checkout."
- **Acceptance Criteria**:
  - [ ] `docker/Dockerfile` copies `config/cloud-init/` (and only that
    subdirectory of `config/`) into the image at `/app/config/cloud-init/`.
  - [ ] The Dockerfile build fails if `/app/config/cloud-init/*.yaml` is
    missing after the copy (build-time self-check).
  - [ ] `.dockerignore` does not exclude `config/` (verified: it doesn't
    today; no change needed, but confirmed as part of this use case so a
    future edit doesn't silently reintroduce the gap).
  - [ ] A `_create_droplet` call with `DO_CLOUD_INIT` configured and the
    file present reads its contents and passes them as `user_data=` to the
    `digitalocean.Droplet(...)` constructor, unchanged from today's behavior.

## SUC-002: Node Creation Aborts Cleanly When Cloud-Init Is Misconfigured
Parent: UC-006 (Operator Adds a Swarm Node via CLI)

- **Actor**: Admin / Operator; also exercised automatically by the
  autoscaler's scale-up path.
- **Preconditions**: `DO_CLOUD_INIT` (or `DO_CLOUD_INIT_FILE`) is set in
  config, but the resolved file does not exist or cannot be read (e.g. a
  future Dockerfile regression, a typo'd filename, a broken deploy).
- **Main Flow**:
  1. Operator (or the autoscaler) triggers node creation.
  2. `_create_droplet` resolves the configured cloud-init path.
  3. The file is missing or unreadable.
  4. `_create_droplet` raises `click.ClickException` describing the
     resolved path and remediation, **before** uploading any SSH key to
     DigitalOcean or calling `droplet.create()`.
- **Postconditions**: No droplet is created. No DigitalOcean side effects
  occurred. The operator (or autoscaler log) sees a clear, actionable error
  instead of a silent warning followed by a bare, unprovisioned node.
- **Error Flows**:
  - `DO_CLOUD_INIT` unset entirely: **not an error** — this is an explicit
    operator opt-out; node creation proceeds without `user_data`, logging
    an informational message, exactly as today.
- **Acceptance Criteria**:
  - [ ] Configured-but-missing file: `_create_droplet` raises
    `click.ClickException` and makes no DigitalOcean API call
    (`Droplet(...).create()` never invoked).
  - [ ] Unset `DO_CLOUD_INIT`: `_create_droplet` proceeds with
    `user_data=None`, no exception (regression guard for the intentional
    opt-out case).
  - [ ] The exception message names the resolved path that was checked.

## SUC-003: A Defective Node Is Detected After Joining and Never Receives Hosts
Parent: UC-006 (Operator Adds a Swarm Node via CLI)

- **Actor**: Admin / Operator (`node expand`); the autoscaler control loop
  (`cspawn/cs_docker/autoscale.py::apply_plan`, unattended/cron-triggered).
- **Preconditions**: A node has just been created, configured, and joined
  to the swarm (via `_create_droplet` → `_configure_node` → `_join_swarm`),
  regardless of whether its cloud-init actually ran to completion
  correctly (e.g. a transient failure mid-boot, an SSH/UFW race, or a
  docker-ce pin that didn't take).
- **Main Flow**:
  1. Immediately after the node is confirmed present in
     `docker node ls`, the caller runs `_verify_node_provisioning`.
  2. The check attempts several consecutive SSH connects.
  3. The check runs `docker --version` over SSH and compares it against the
     version pinned in the configured cloud-init file (parsed via regex from
     the `DOCKER_PIN=` line — no new config key).
  4. The check runs `cloud-init status` over SSH and confirms `status: done`.
  5. If all three checks pass, the node is reported healthy and the calling
     flow proceeds/completes normally.
  6. If any check fails, the caller logs the failure(s) at `ERROR`,
     best-effort drains the node (`_find_swarm_node` + `_drain_swarm_node`)
     so Swarm's scheduler stops considering it, and:
     - `node expand`: raises `click.ClickException` (non-zero CLI exit).
     - autoscaler `apply_plan`: records the failure in `ApplyResult.errors`,
       does not increment `result.added`, and continues to the next planned
       node in the batch.
- **Postconditions**: A node that failed post-join verification is drained
  (unschedulable) and its failure is visible in logs/`ApplyResult` — it can
  never silently start receiving code-server hosts.
- **Acceptance Criteria**:
  - [ ] `_verify_node_provisioning` returns a list of human-readable failure
    strings (empty list = healthy); it does not raise for expected failure
    modes (SSH down, version mismatch, cloud-init not done).
  - [ ] `node expand` calls verification after confirming swarm membership;
    on failure, drains the node and exits non-zero.
  - [ ] `apply_plan`'s scale-up loop calls verification after `_join_swarm`
    for each newly created node; on failure, drains the node, records the
    error, skips counting it as added, and continues with the next planned
    node rather than aborting the whole batch.
  - [ ] When `expected_docker_version` cannot be determined (no cloud-init
    configured), the version check is skipped rather than producing a false
    failure.
