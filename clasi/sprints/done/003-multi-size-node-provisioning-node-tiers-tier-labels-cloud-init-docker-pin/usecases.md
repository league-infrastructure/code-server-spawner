---
status: draft
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 003 Use Cases

## SUC-001: Operator provisions a node with a non-default size tier

- **Actor**: Operator (via CLI)
- **Preconditions**: `NODE_TIERS` is configured with at least two named tiers; `DEFAULT_TIER` is set; manager is reachable via `DOCKER_URI`.
- **Main Flow**:
  1. Operator runs `cspawnctl node expand --tier large`.
  2. CLI resolves the `large` tier from `NODE_TIERS` to obtain the DO slug and capacity.
  3. CLI creates a DigitalOcean droplet with the `large` slug.
  4. Cloud-init pins docker-ce to the manager's version during first boot.
  5. CLI joins the new node to the swarm.
  6. CLI stamps `cs.tier=large` and `cs.capacity=14` labels on the node.
- **Postconditions**: A new worker node with the large slug appears in `docker node ls`; it carries `cs.tier=large` and `cs.capacity=14` labels.
- **Acceptance Criteria**:
  - [ ] `cspawnctl node expand --tier large` creates a droplet with the large DO slug.
  - [ ] `--tier` with an invalid name produces a descriptive error.
  - [ ] `cspawnctl node expand` (no `--tier`) uses `DEFAULT_TIER`.
  - [ ] `cs.tier` and `cs.capacity` labels are present on the newly joined node.

## SUC-002: Operator provisions a node without NODE_TIERS configured (backward compat)

- **Actor**: Operator (via CLI)
- **Preconditions**: `NODE_TIERS` is absent from config; `DO_SIZE` and `DEFAULT_CAPACITY` are set.
- **Main Flow**:
  1. Operator runs `cspawnctl node expand` (no `--tier`).
  2. CLI synthesizes a single default tier from `DO_SIZE` and `DEFAULT_CAPACITY`.
  3. Node is created, joined, and labeled as usual.
- **Postconditions**: Provisioning succeeds identically to the current behavior.
- **Acceptance Criteria**:
  - [ ] Provisioning succeeds when `NODE_TIERS` is absent.
  - [ ] `cs.tier=default` and `cs.capacity=<DEFAULT_CAPACITY>` labels are applied.

## SUC-003: New node joins swarm without Docker version mismatch

- **Actor**: Automated provisioning (cloud-init + spawner)
- **Preconditions**: Manager runs docker-ce 27.4.1. Cloud-init has been updated to pin docker-ce.
- **Main Flow**:
  1. Droplet is created with cloud-init user-data.
  2. Cloud-init installs docker-ce 27.4.1 (pinned) instead of `docker.io`.
  3. Spawner's join preflight checks major version equality.
  4. Versions match; join proceeds.
- **Postconditions**: Node joins without manual docker downgrade intervention.
- **Acceptance Criteria**:
  - [ ] `config/cloud-init/swarm-node-init-v2.yaml` no longer lists `docker.io` in `packages:`.
  - [ ] `runcmd` installs and holds `docker-ce=5:27.4.1-1~ubuntu.20.04~focal`.
  - [ ] Join preflight in `_join_swarm` passes without raising a version mismatch error.

## SUC-004: Operator backfills tier labels on existing swarm nodes

- **Actor**: Operator (via CLI)
- **Preconditions**: Nodes swarm1–swarm5 exist but lack `cs.tier` / `cs.capacity` labels; `NODE_TIERS` config is in place; DO API token is available.
- **Main Flow**:
  1. Operator runs `cspawnctl node label-backfill` (dry run).
  2. CLI lists swarm nodes, resolves each node's DO droplet, reads its `size_slug`, maps it to a tier via `tier_for_slug`.
  3. CLI prints a table: node | slug | inferred tier | capacity | action (would-apply).
  4. Operator runs `cspawnctl node label-backfill --apply`.
  5. CLI stamps `cs.tier` / `cs.capacity` on each unlabeled node.
- **Postconditions**: All existing swarm nodes have `cs.tier` and `cs.capacity` labels.
- **Acceptance Criteria**:
  - [ ] Dry run prints correct table without modifying any labels.
  - [ ] `--apply` stamps labels on nodes that lack `cs.tier`.
  - [ ] Nodes already carrying `cs.tier` are skipped (idempotent).
  - [ ] Unknown slug is reported with a warning; node is skipped (not aborted).

## SUC-005: Operator contracts the cluster by removing an empty node

- **Actor**: Operator (via CLI)
- **Preconditions**: At least one worker node has zero running code-server hosts; `cs.capacity` labels are present (or `DEFAULT_CAPACITY` fallback is configured).
- **Main Flow**:
  1. Operator runs `cspawnctl node contract`.
  2. CLI queries live running-host counts per node via the swarm task list.
  3. CLI identifies empty worker nodes.
  4. CLI selects the candidate: smallest `cs.capacity`, then highest serial (newest) as tiebreaker.
  5. CLI stops the selected node (drain → remove → destroy).
- **Postconditions**: The selected empty node is removed from the swarm and destroyed in DO.
- **Acceptance Criteria**:
  - [ ] `contract` only removes a node with zero running hosts.
  - [ ] When no empty node exists, contract prints "No empty node to contract" and exits cleanly.
  - [ ] Among multiple empty nodes, the one with smallest `cs.capacity` (then highest serial) is selected.
  - [ ] `--dry-run` prints the candidate without destroying it.
  - [ ] `_select_contract_candidate` is an extractable function reusable by a future autoscaler.
