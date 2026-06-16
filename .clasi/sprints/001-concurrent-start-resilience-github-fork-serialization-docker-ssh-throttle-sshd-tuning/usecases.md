---
status: draft
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 001 Use Cases

## SUC-001: Concurrent Students Start Hosts Without GitHub Fork Errors
Parent: UC-003

- **Actor**: Multiple students (up to 20) clicking "Start" simultaneously
- **Preconditions**: A class is in `running` state; up to 20 students are
  enrolled; all students share the same upstream GitHub repo (e.g.
  `Python-Apprentice`); none has an existing active host.
- **Main Flow**:
  1. 20 students click "Start" in close succession (within seconds of each other).
  2. Each request calls `GithubOrg.fork(upstream_url, username)`.
  3. Concurrent forks of the same upstream are serialized: the first thread
     acquires the per-upstream lock and calls `upstream_repo.create_fork(...)`;
     subsequent threads queue on the lock and retry if they encounter a
     transient 403 "Repository is already being forked" or 429 secondary
     rate-limit response.
  4. Each fork is renamed uniquely per student (`Python-Apprentice-{username}`)
     via the existing `_rename_with_retry` helper.
  5. Each student's fork is available before the corresponding Docker service
     is created.
- **Postconditions**: All 20 students have uniquely named GitHub forks; no
  403 or 429 errors propagate to the web layer.
- **Acceptance Criteria**:
  - [ ] `cspawnctl test start --concurrency 20` produces 0 GitHub 403/429 failures.
  - [ ] Each of 20 students gets a uniquely named fork in the League-Students org.
  - [ ] A single-student web Start (no concurrency) still succeeds and completes
        without noticeable latency increase.

---

## SUC-002: Concurrent Students Start Hosts Without SSH Overrun
Parent: UC-003

- **Actor**: Multiple students (up to 20) clicking "Start" simultaneously
- **Preconditions**: The Docker manager is reachable over SSH
  (`ssh://root@swarm1`); all host operations share a single `DockerClient`
  instance; up to 20 threads are attempting Docker calls concurrently.
- **Main Flow**:
  1. 20 threads call `CodeServerManager` methods (`new_cs`, `list`, `get`,
     `get_by_username`) concurrently.
  2. The app-level `BoundedSemaphore` (default capacity 4) limits simultaneous
     active SSH/Docker connections to the manager.
  3. Threads that cannot acquire the semaphore immediately block and wait in
     a queue; they proceed once a slot is released.
  4. Each Docker service is created successfully; each `CodeHost` DB record
     is committed.
  5. No `kex_exchange_identification` or `BrokenPipe` errors are raised.
- **Postconditions**: All 20 hosts are created and recorded; the SSH channel
  to the manager is never saturated.
- **Acceptance Criteria**:
  - [ ] `cspawnctl test start --concurrency 20` shows no SSH
        `kex_exchange_identification` / BrokenPipe errors.
  - [ ] All 20 `CodeHost` records are created with state `starting` or better.
  - [ ] Single-student web Start still works and is not noticeably slower.
  - [ ] No deadlock: `new_cs` calls `get_by_username` internally on a 409
        path; this must not deadlock under the semaphore.

---

## SUC-003: New Swarm Nodes Inherit Higher sshd Connection Limits
Parent: UC-006

- **Actor**: Admin / Operator provisioning a new swarm node
- **Preconditions**: The operator runs `cspawnctl node expand` or manually
  boots a node with the cloud-init config.
- **Main Flow**:
  1. A new DigitalOcean droplet is provisioned using `swarm-node-init-v2.yaml`.
  2. cloud-init writes an sshd_config snippet setting `MaxStartups 30:60:100`.
  3. sshd is restarted so the new limit takes effect immediately.
  4. The node joins the swarm; it can accept up to 30 simultaneous SSH
     handshakes before starting to probabilistically reject new ones.
- **Postconditions**: The new node's sshd tolerates burst SSH connections
  without dropping them below the 30-simultaneous threshold.
- **Acceptance Criteria**:
  - [ ] `swarm-node-init-v2.yaml` contains `MaxStartups 30:60:100`.
  - [ ] `manager-setup-swarm.sh` contains `MaxStartups 30:60:100` and a
        comment noting the manual step required for existing `swarm1`.
  - [ ] UFW port-22 rule is reviewed: current scripts use `ufw allow 22/tcp`
        (not `limit`), so no rate-limit rule needs removal; this is confirmed
        and documented in the ticket.
