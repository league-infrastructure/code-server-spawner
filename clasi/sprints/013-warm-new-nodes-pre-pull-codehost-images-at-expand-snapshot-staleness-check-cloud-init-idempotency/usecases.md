---
status: approved
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 013 Use Cases

## SUC-001: Operator Expands the Fleet Without a Cold-Pull 503 Herd
Parent: UC-006 (Operator Adds a Swarm Node via CLI)

- **Actor**: Admin / Operator (via `cspawnctl node expand`) and, equivalently,
  the autoscaler acting on the operator's behalf (`apply_plan`'s scale-up
  loop, triggered by the cron-driven `run_autoscale`).
- **Preconditions**: A new droplet has been created, configured, and has
  joined the Docker Swarm as a worker (UC-006 steps 1-6 complete); the swarm
  manager is reachable; at least one `ClassProto` row exists with an
  `image_uri`.
- **Main Flow**:
  1. Immediately after the node is confirmed present in swarm membership,
     the system sets the node's availability to `drain` (idempotent — a
     no-op if the node somehow already reports drained).
  2. The existing post-join `_verify_node_provisioning` hard gate runs
     unchanged. If it fails, the node stays drained and the command/batch
     item aborts (existing sprint-009/012 behavior, unmodified).
  3. If verification passes, the system resolves the set of images to warm:
     `SELECT DISTINCT image_uri FROM class_proto`, unioned with the optional
     `NODE_PREPULL_IMAGES` config allowlist.
  4. For each image, the system runs `ssh root@<node-fqdn> docker pull
     <image>` (the node has no docker CLI reachable from the spawner
     container itself — this executes node-local, mirroring the pattern
     `CodeHostRepo.push()` uses for `docker exec`), bounded by a per-image
     timeout. A failed or timed-out pull logs a WARNING and does not abort
     the sequence.
  5. Once all image pulls have been attempted (regardless of individual
     success/failure), the system sets the node's availability back to
     `active`.
- **Postconditions**: The node is schedulable only after its image cache has
  been warmed (or a best-effort attempt has been made and logged); no host
  can be scheduled onto the node between steps 1 and 5, since it is drained
  throughout that window.
- **Acceptance Criteria**:
  - [ ] Drain is set before any pull attempt, and active is set only after
    all pull attempts complete, in both `expand()` and `apply_plan()`'s
    scale-up loop (assert the ordering; not just presence, of both calls).
  - [ ] The image set pulled is the union of distinct `class_proto.image_uri`
    values and the configured `NODE_PREPULL_IMAGES` allowlist (when set).
  - [ ] A pull failure (non-zero exit, timeout, or SSH error) for one image
    logs a WARNING, does not raise, and does not block subsequent images or
    prevent reactivation.
  - [ ] A wedged/hanging pull is bounded by a timeout and cannot hang the
    `expand` command or the autoscaler's scale-up batch indefinitely.
  - [ ] `apply_plan`'s scale-up loop exhibits the identical drain→pull→
    activate behavior as `expand()` — this is a new, explicit wiring in
    autoscale.py, not an automatic consequence of any shared code path
    (`apply_plan` does not call `expand()`; see architecture-update.md).

---

## SUC-002: Operator Is Warned When a Node's Baked Docker Has Drifted From a Golden Snapshot
Parent: UC-006 (Operator Adds a Swarm Node via CLI)

- **Actor**: Admin / Operator.
- **Preconditions**: A node has just joined the swarm and post-join
  verification has run (pass or fail); the manager's live docker-ce version
  is determinable via `_manager_docker_version`.
- **Main Flow**:
  1. The system compares the new node's docker-ce major version (read via
     SSH `docker --version`) against the manager's live major version.
  2. When the majors differ, the system logs a WARNING naming the likely
     cause ("this node may have been provisioned from a golden snapshot
     whose baked docker-ce has drifted from the manager's current version")
     and the concrete remedy (`scripts/build-golden-node-snapshot.sh`,
     referencing `docs/golden-node-snapshot.md`).
  3. This WARNING is independent of, and additional to, the existing
     `_verify_node_provisioning` hard-gate failure that a real major
     mismatch already triggers (unchanged pass/fail semantics; unchanged
     drain-and-abort behavior on failure).
- **Postconditions**: An operator reading the expand/autoscale logs after a
  major-mismatch incident sees not just "docker version mismatch: expected
  X, got Y" but a clear, named likely cause and an actionable next step.
- **Acceptance Criteria**:
  - [ ] A docker major mismatch produces a WARNING-level log line naming
    "golden snapshot" staleness as the likely cause and
    `scripts/build-golden-node-snapshot.sh` as the remedy, in addition to
    the existing verification failure — it does not replace or alter the
    existing failure message or the hard-gate behavior.
  - [ ] No WARNING is logged when the majors match.
  - [ ] The check never raises and never itself blocks node activation or
    the expand/autoscale flow — it is purely diagnostic logging.

---

## SUC-003: Cloud-Init Skips the Docker-CE Pin Round-Trip on an Already-Correct Node
Parent: UC-006 (Operator Adds a Swarm Node via CLI)

- **Actor**: System (cloud-init, running at first boot on a new droplet).
- **Preconditions**: The droplet has booted from either (a) the golden
  snapshot (docker-ce pre-installed and held at the manager's version at
  snapshot-build time) or (b) a bare base image (no docker-ce installed).
- **Main Flow**:
  1. Before running the sprint-012 hardened install sequence, cloud-init
     checks whether `docker --version` is already present and whether its
     major matches the resolved `DOCKER_PIN`'s major.
  2. If docker is present and its major matches: the entire
     stop/mask-contenders → apt-get update → install-with-retry →
     apt-mark hold → unmask/re-enable sequence is skipped. A clear log line
     is written noting the skip and the matched version.
  3. If docker is absent, or present with a different major: the complete
     sprint-012 hardened sequence runs unchanged, including the fail-loud
     marker/exit-1 path if the install does not converge on the pin's
     major.
- **Postconditions**: A golden-snapshot node's boot avoids an unnecessary
  `apt-get update` + install/hold round-trip; a non-snapshot or
  wrong-major node is provisioned exactly as sprint 012 already hardened it.
- **Acceptance Criteria**:
  - [ ] When `docker --version`'s major equals the pin's major, none of the
    stop/mask, `apt-get install docker-ce=...`, or unmask/re-enable commands
    execute (content/structure assertion on the rendered YAML).
  - [ ] When docker is absent or its major differs from the pin, the full
    hardened install (lock-guard, retry loop, hold, fail-loud assertion,
    unmask/re-enable) is present and unchanged from sprint 012.
  - [ ] The fail-loud marker/exit-1 path for a wrong-major post-install
    result is preserved in the fall-through branch.
  - [ ] Tests assert both branches exist in the rendered cloud-init content.
