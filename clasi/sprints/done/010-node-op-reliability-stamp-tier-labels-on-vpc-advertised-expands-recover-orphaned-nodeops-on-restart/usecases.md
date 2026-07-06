---
status: approved
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 010 Use Cases

## SUC-001: `node expand` Stamps cs.tier/cs.capacity on a VPC-Advertised Swarm
Parent: UC-006 (Operator Adds a Swarm Node via CLI)

- **Actor**: Admin / Operator (`cspawnctl node expand --tier <t>`); the
  autoscaler's scale-up path (`cs_docker/autoscale.py::apply_plan`), which
  calls the same `_join_swarm` with a resolved `tier`.
- **Preconditions**: A node has just joined the swarm via `_join_swarm` with
  a known `tier`. The swarm's data-plane is VPC-based, so the node joined
  with `--advertise-addr <10.124.x.x>` — every deployment today. Docker
  Swarm therefore reports the node's `Status.Addr` as its **private** VPC
  address, never its DigitalOcean public IP.
- **Main Flow**:
  1. `_join_swarm` completes `docker swarm join` on the droplet.
  2. The tier-labeling step locates the just-joined node in
     `manager_client.nodes.list()` by **hostname** (the short name set by
     `_configure_node`'s `hostnamectl set-hostname`), using the same
     `_find_swarm_node` lookup already used elsewhere in this file (drain,
     label-backfill) — not by comparing `Status.Addr` to the droplet's
     public IP.
  3. Once found, `cs.tier=<tier.name>` and `cs.capacity=<tier.capacity>`
     are written via `_ensure_node_labels`.
- **Postconditions**: The node carries `cs.tier`/`cs.capacity` labels
  immediately after `expand` returns — no manual `node label-backfill
  --apply` required. `tiers.py::node_capacity()` reads the real capacity
  instead of falling back to `DEFAULT_CAPACITY` (6), so a large-tier node
  (capacity 14) is counted correctly by the autoscaler from the moment it
  joins.
- **Acceptance Criteria**:
  - [ ] A full `node expand --tier <t>` run against a mocked manager client
    whose `Status.Addr` is a private VPC address (different from the
    droplet's public IP) results in `cs.tier`/`cs.capacity` being written.
  - [ ] The admin Nodes tab shows Tier/Capacity for a newly expanded node
    without any manual backfill step.
  - [ ] No change to the existing `code-host-user` labeling block's
    behavior (out of scope — see architecture-update.md).

## SUC-002: Label-Application Timeout Is Logged, Not Silently Swallowed
Parent: UC-006 (Operator Adds a Swarm Node via CLI)

- **Actor**: Admin / Operator; the autoscaler's scale-up path.
- **Preconditions**: The just-joined node never appears (by hostname) in
  `manager_client.nodes.list()` within the polling deadline — e.g. Docker
  Swarm is slow to propagate membership, or the node failed to join for an
  unrelated reason.
- **Main Flow**:
  1. The tier-labeling step polls for up to 90s trying to find the node by
     hostname.
  2. The deadline passes without a match.
  3. A `WARNING`-level log line is emitted, naming the target node and the
     labels that were not applied.
- **Postconditions**: The operator (or autoscaler log) has a visible signal
  that a node is missing its tier labels, instead of a silent `except:
  pass` that was invisible for an unknown number of node generations (see
  issue `expand-tier-labels-never-stamp.md`).
- **Acceptance Criteria**:
  - [ ] When the node never appears within the deadline, a `WARNING` is
    logged naming the node and the labels that were not applied.
  - [ ] The function never raises for this expected failure mode — it
    returns `False` so callers can decide how to react (matches this
    file's existing convention, e.g. `_ensure_node_labels`).

## SUC-003: A Spawner Container Restart Never Leaves a NodeOp Stuck "running"
Parent: sprint 006 SUC-002 (Admin starts a new node) / SUC-004 (Admin
monitors live operation status and log)

- **Actor**: Admin / Operator (observes the Nodes tab); the spawner
  process itself (Gunicorn master, via `cspawn/app.py`'s single
  `preload_app` import).
- **Preconditions**: An admin-triggered `NodeOp` (expand/remove/rebalance)
  is mid-flight — its row has `status='running'` — when the spawner
  container is killed and restarted (deploy, OOM, host reboot). The
  detached `op-run` subprocess dies with the container; its `finally`
  block, which would have written a terminal status, never executes.
- **Main Flow**:
  1. The container restarts. Gunicorn's `preload_app` re-imports
     `cspawn/app.py`, which calls `init_app(sweep_node_ops=True)` exactly
     once for the new process (workers are forked from this preloaded
     app, so the sweep does not repeat per-worker).
  2. `init_app` calls `sweep_interrupted_node_ops(app)`, which finds every
     `NodeOp` row with `status='running'` — a state that cannot legitimately
     survive a container restart, since no detached `op-run` subprocess can
     outlive the container it was spawned in.
  3. Each such row is updated: `status='interrupted'`, `exit_code=1`,
     `message` explaining the restart (naming the orphaned droplet, per
     SUC-004, when one was recorded), `finished_at=now()`.
  4. Rows already in a terminal state (`done`, `failed`, `interrupted`) or
     still `pending` are left untouched.
- **Postconditions**: No `NodeOp` row is ever stuck `running` forever. The
  admin Nodes tab reflects reality on the next page load after a restart.
- **Error Flows**:
  - A `cspawnctl` CLI invocation (including the `op-run` subprocess itself,
    and routine commands like `node info`/`node autoscale`) does **not**
    trigger this sweep — only the one true process-boot call site
    (`cspawn/app.py`) does. See architecture-update.md's design rationale
    for why this distinction is load-bearing (a CLI-triggered sweep would
    race with, and falsely interrupt, a genuinely in-flight op).
- **Acceptance Criteria**:
  - [ ] A `NodeOp` row with `status='running'` becomes `status='interrupted'`
    (`exit_code=1`, `message` set, `finished_at` set) after
    `sweep_interrupted_node_ops(app)` runs.
  - [ ] Rows in `pending`, `done`, `failed`, or already-`interrupted` states
    are untouched by the sweep.
  - [ ] `init_app(sweep_node_ops=False)` (the default, used by every CLI
    call site via `cspawn/cli/util.py::get_app`) does not invoke the sweep.
  - [ ] `cspawn/app.py` (the Gunicorn/dev-server entrypoint) is the only
    call site passing `sweep_node_ops=True`.

## SUC-004: An Interrupted Expand Names the Droplet It May Have Orphaned
Parent: sprint 006 SUC-002 (Admin starts a new node)

- **Actor**: Admin / Operator, diagnosing an `interrupted` op after a
  restart.
- **Preconditions**: An `expand` `NodeOp` reached the point where
  `_create_droplet` successfully created a DigitalOcean droplet, but the
  container was killed before `_configure_node`/`_join_swarm` completed
  (the droplet exists in DigitalOcean but never joined the swarm — an
  orphaned, billed, invisible-to-the-cluster resource).
- **Main Flow**:
  1. `op-run` invokes `expand(..., node_op_id=<op.id>)` for `kind='expand'`
     ops, inside an active Flask app context.
  2. As soon as `_create_droplet` returns a created droplet, it best-effort
     records `droplet_id` and `target_fqdn` on the `NodeOp` row identified
     by `node_op_id` (no-op when `node_op_id` is not supplied — e.g. a
     bare CLI `node expand` run outside the admin UI).
  3. The container is killed before the op reaches a terminal state.
  4. On restart, `sweep_interrupted_node_ops` (SUC-003) composes a message
     naming the recorded `droplet_id`/`target_fqdn` as a possible orphan
     needing manual cleanup.
- **Postconditions**: The operator sees, in the op's message (surfaced in
  the admin UI per SUC-005), which specific droplet to check for and
  possibly destroy — instead of having to cross-reference DigitalOcean's
  droplet list against swarm membership by hand.
- **Acceptance Criteria**:
  - [ ] `_create_droplet` accepts an optional `node_op_id` and, when given,
    writes `droplet_id`/`target_fqdn` onto the matching `NodeOp` row
    immediately after droplet creation succeeds.
  - [ ] `op-run` passes `node_op_id=op.id` into `expand(...)` only for
    `kind='expand'`, wrapped in an app context.
  - [ ] A DB-write failure inside this best-effort recording never aborts
    node creation (logs a warning; `expand` proceeds normally).
  - [ ] An interrupted op whose droplet was recorded surfaces the
    `droplet_id`/`target_fqdn` in its `message`; an interrupted op with no
    recorded droplet (interrupted before `_create_droplet` returned, or a
    non-`expand` op) gets the generic restart message with no orphan claim.

## SUC-005: Admin Nodes Tab Renders an Interrupted Op Distinctly
Parent: sprint 006 SUC-004 (Admin monitors live operation status and log)

- **Actor**: Admin / Operator viewing `/admin/nodes`.
- **Preconditions**: One or more `NodeOp` rows have `status='interrupted'`
  (per SUC-003).
- **Main Flow**:
  1. The Nodes tab's recent-operations table renders each op's status
     badge; `interrupted` gets its own distinct color, separate from
     `pending`/`running` (which still spin/poll) and from `done`/`failed`.
  2. The row's status badge carries the op's `message` (e.g., naming an
     orphaned droplet per SUC-004) as a tooltip, so the operator does not
     need to open the raw per-op log file to see it.
  3. The page's polling JavaScript does not attempt to poll an
     `interrupted` row (it is already terminal, like `done`/`failed`).
- **Postconditions**: An operator scanning the Nodes tab can immediately
  tell a restart-interrupted op apart from one that is still genuinely
  in-flight — no more phantom "running" spinners.
- **Acceptance Criteria**:
  - [ ] `interrupted` renders with a badge class distinct from
    `done`/`failed`/`running`/`pending`.
  - [ ] An `interrupted` op's `message` is visible in the rendered page
    (not only via the JSON status endpoint).
  - [ ] `interrupted` is excluded from the client-side poll-trigger
    condition (only `pending`/`running` rows poll).
