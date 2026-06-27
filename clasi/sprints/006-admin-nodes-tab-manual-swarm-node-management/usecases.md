---
sprint: '006'
status: final
---

# Use Cases — Sprint 006: Admin Nodes Tab

## SUC-001: Admin lists swarm nodes with host counts

**Actor**: Admin user

**Preconditions**:
- Admin is authenticated and holds admin role.
- Swarm manager is reachable via `DOCKER_URI`.

**Trigger**: Admin navigates to `/admin/nodes`.

**Main flow**:
1. The route opens a fresh Docker client (not the app-level cached client).
2. It calls `count_hosts_per_node(client)` to get running code-server task counts per node.
3. It calls `client.nodes.list()` to enumerate all swarm nodes.
4. It reads tier labels (`cs.tier`, `cs.capacity`) from each node's Spec.Labels.
5. It renders the Nodes tab with a table showing: hostname, IP (`Status.Addr`), role (manager/worker/leader), tier name, capacity, host count, availability state.
6. Start buttons (one per tier from `load_tiers(cfg)`) and a Remove button per worker node are shown.

**Postconditions**: Admin sees an accurate, live node table. Manager/leader nodes do not show a Remove button (client-side) and are refused server-side.

**Error path**: If the Docker client cannot connect, the route renders an error flash message and an empty table.

---

## SUC-002: Admin starts a new node

**Actor**: Admin user

**Preconditions**:
- Admin is authenticated.
- Config keys `DO_TOKEN`, `DO_NAMES`, `DOCKER_URI`, `DATA_DIR`, `JTL_DEPLOYMENT` are set.
- At least one tier is defined via `NODE_TIERS` or `DO_SIZE`.

**Trigger**: Admin clicks "Start large" or "Start small" on the Nodes tab, which submits `POST /admin/nodes/start` with field `tier`.

**Main flow**:
1. Route validates the `tier` field against `load_tiers(cfg)`.
2. Creates a `NodeOp` row: `kind='expand'`, `tier=<name>`, `status='pending'`, `log_path=<DATA_DIR>/node-ops/<id>.log`.
3. Commits the row to the database.
4. Launches `cspawnctl -d <deploy> node op-run <op_id>` as a detached subprocess (`start_new_session=True`, stdout/stderr to DEVNULL).
5. Flashes a success message with the op ID.
6. Redirects back to `/admin/nodes`.

**Postconditions**: A `NodeOp` row is created with `status='pending'`. A background subprocess begins provisioning the node. Admin can monitor progress via the Operations panel (SUC-004).

**Error path**: If `tier` is invalid, route flashes an error and redirects without creating a `NodeOp`.

---

## SUC-003: Admin removes a node

**Actor**: Admin user

**Preconditions**:
- Admin is authenticated.
- Target node is a worker (not manager, not leader).
- `fqdn` of the target node is known.

**Trigger**: Admin clicks "Remove" for a worker node (JS `confirm()` dialog confirms intent), submitting `POST /admin/nodes/remove` with field `fqdn`.

**Main flow**:
1. Route queries swarm to confirm the target node is not a manager or leader. If it is, route flashes an error and redirects.
2. Creates a `NodeOp` row: `kind='remove'`, `target_fqdn=<fqdn>`, `status='pending'`, `log_path=<DATA_DIR>/node-ops/<id>.log`.
3. Commits the row to the database.
4. Launches `cspawnctl -d <deploy> node op-run <op_id>` as a detached subprocess.
5. Flashes a success message with the op ID.
6. Redirects back to `/admin/nodes`.

**Postconditions**: A `NodeOp` row is created with `status='pending'`. A background subprocess begins draining, removing from swarm, and destroying the droplet. Admin can monitor progress via SUC-004.

**Error path**: Attempting to remove a manager or leader node is refused server-side with a flash error. No `NodeOp` is created.

---

## SUC-004: Admin monitors live operation status and log

**Actor**: Admin user

**Preconditions**:
- One or more `NodeOp` rows exist (status pending, running, done, or failed).

**Trigger**: Admin views the Nodes tab while operations are running or recently completed.

**Main flow**:
1. The page renders an Operations panel showing all recent `NodeOp` rows (running first, then last N completed).
2. For each in-progress op, JavaScript polls `GET /admin/nodes/op/<id>/status` every 2 seconds.
3. The status route returns JSON: `{status, exit_code, message, log_tail}` (last ~50 lines of the log file).
4. The UI updates the op row in place: status badge, message, log tail.
5. When an op transitions to `done` or `failed`, polling stops for that op and the node table refreshes.
6. Admin can click a "Full log" link to view the complete op log via `GET /admin/nodes/op/<id>/log`.

**Postconditions**: Admin has real-time visibility into node operation progress without a page refresh.

**Error path**: If the log file is not yet created (op just started), the tail returns an empty string. If the op ID is not found, the route returns 404.
