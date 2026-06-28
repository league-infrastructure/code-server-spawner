# Sprint 006 — Prod Smoke-Test Checklist

Manual one-time verification of the admin Nodes tab after deploying sprint 006
to the production container. Execute these steps in order; do not skip any
pre-conditions.

---

## Pre-conditions (one-time setup, verify before first live run)

### 1. Deploy and migrate

```bash
# Build and push the new image, then pull it on the swarm manager
docker pull registry.digitalocean.com/jtl-containers/cspawn:latest

# Apply the NodeOp migration inside the running container
docker exec codeserver_codeserver flask db upgrade
```

Confirm the migration ran:

```bash
docker exec codeserver_codeserver flask db current
# Should show: v006_add_node_op_table (head)
```

### 2. Verify SSH key is registered with DigitalOcean

The `expand` command injects all DigitalOcean SSH keys into new droplets at
creation time. The prod container's key (`/root/.ssh/id_rsa.pub`) **must** be
registered as a DigitalOcean SSH key before a new droplet can accept SSH
connections from the spawner.

Check the registered keys:

```bash
# Inside the prod container
cspawnctl -d prod node info
# Look for "SSH keys:" line listing registered key names and fingerprints
```

Alternatively check via the DigitalOcean control panel:
Settings -> Security -> SSH keys -- look for a key named `cspawn-*` or matching
the fingerprint of `/root/.ssh/id_rsa.pub`.

If the key is **missing**, register it manually:

```bash
# Read the public key
cat /root/.ssh/id_rsa.pub

# Register via doctl (or paste in the DO control panel)
doctl compute ssh-key import cspawn-prod --public-key-file /root/.ssh/id_rsa.pub
```

### 3. Verify `_ensure_priv_key` resolves correctly

```bash
docker exec codeserver_codeserver python3 -c "
from cspawn.cli.node import _ensure_priv_key
priv, pub = _ensure_priv_key()
print('priv:', priv, 'exists:', priv.exists())
print('pub:', pub, 'exists:', pub.exists())
"
```

Expected: both paths should resolve to `/root/.ssh/id_rsa` and
`/root/.ssh/id_rsa.pub`.

---

## Smoke-Test Procedure

### Step 1 -- Log in to the prod admin UI

Navigate to `https://code.jtl.codes/admin/nodes` (or the prod domain). Log in
with an admin account.

- [ ] The Nodes tab loads without a 500 error.
- [ ] The node table shows the current swarm members (at minimum the manager
      node) with their roles, IP addresses, and host counts.

### Step 2 -- Start a new small node

Click **"Start small node"** in the "Add a Node" section.

- [ ] The page redirects back to `/admin/nodes` after submission.
- [ ] The Operations panel shows a new op entry with `status: pending` or
      `status: running`.
- [ ] The log tail area updates approximately every 2 seconds (JS polling).
- [ ] The op log contains `[expand]` prefixed lines showing droplet creation
      progress.

Wait approximately 90 seconds for provisioning to complete.

- [ ] The op entry in the Operations panel shows `status: done`.
- [ ] A new node row appears in the Nodes table (e.g., `swarm3.jtl.codes`).
- [ ] The new node's role is `worker` and availability is `active`.

### Step 3 -- Verify DNS for the new node

```bash
dig swarm3.jtl.codes +short   # substitute the actual new hostname
```

- [ ] Returns the new droplet's public IPv4 address.

Also confirm in the DigitalOcean control panel:
- [ ] New droplet exists in the Droplets list with the expected name.
- [ ] Droplet is tagged with the project tag (e.g., `jtl-codeserver`).

### Step 4 -- Remove the new node

In the Nodes table, locate the newly added worker and click **"Remove"**. Confirm
the JavaScript dialog that appears.

- [ ] A new `remove` op appears in the Operations panel with `status: running`.
- [ ] The log tail shows drain, Docker node removal, and droplet destroy steps.
- [ ] The op completes with `status: done`.
- [ ] The node row disappears from the Nodes table.

### Step 5 -- Verify droplet is destroyed

- [ ] In the DigitalOcean control panel, the new droplet is no longer listed.
- [ ] DNS record for `swarm3.jtl.codes` (or whichever node was removed) is gone
      or no longer resolves to the old IP.

### Step 6 -- Post-run cleanup check

Check for stuck ops:

```bash
docker exec codeserver_codeserver flask shell -c "
from cspawn.models import NodeOp, db
stuck = NodeOp.query.filter_by(status='running').all()
print(f'Stuck running ops: {len(stuck)}')
for op in stuck:
    print(f'  {op.id} {op.kind} started_at={op.started_at}')
"
```

- [ ] No `NodeOp` rows remain in `status='running'` after cleanup.

---

## Op Log Locations

Node operation logs are written to:

```
{DATA_DIR}/node-ops/<op_id>.log
```

In prod the `DATA_DIR` is typically `/data`. So a log file lives at:

```
/data/node-ops/<uuid>.log
```

Read a log directly:

```bash
docker exec codeserver_codeserver cat /data/node-ops/<op_id>.log
```

Or use the admin UI full-log link: `/admin/nodes/op/<op_id>/log`.

---

## Reading Op Status

Via the admin UI: `/admin/nodes` -> Operations panel.

Via the API: `GET /admin/nodes/op/<op_id>/status` returns:

```json
{
  "status": "done",
  "exit_code": 0,
  "message": null,
  "log_tail": "...(last 50 lines of log)..."
}
```

Via the database:

```bash
docker exec codeserver_codeserver flask shell -c "
from cspawn.models import NodeOp
ops = NodeOp.query.order_by(NodeOp.created_at.desc()).limit(5).all()
for op in ops:
    print(op.id[:8], op.kind, op.tier or op.target_fqdn, op.status, op.exit_code)
"
```

---

## Expected Lifecycle Summary

### Start (expand) flow

1. Admin clicks "Start X node".
2. Route creates `NodeOp(kind='expand', tier='X', status='pending')`.
3. Route launches `cspawnctl -d prod node op-run <id>` as a detached subprocess.
4. `op-run` loads the op, sets `status='running'`, acquires file lock.
5. `op-run` invokes `expand --tier X`: creates droplet, waits for active,
   SSH-configures, joins swarm, sets labels, updates DNS.
6. `op-run` sets `status='done'`, `exit_code=0`, releases lock.
7. JS polling picks up `status='done'` and stops polling.

### Remove flow

1. Admin clicks "Remove" on a worker row.
2. Route verifies node is a worker (not manager/leader).
3. Route creates `NodeOp(kind='remove', target_fqdn='<fqdn>', status='pending')`.
4. Route launches `cspawnctl -d prod node op-run <id>` detached.
5. `op-run` invokes `stop_node <fqdn>`: drains tasks, removes from swarm,
   destroys droplet, removes DNS record.
6. `op-run` sets `status='done'`.

---

## Smoke-Test Result

> **Status**: PENDING -- to be filled in after the first manual prod run.
>
> Date:
> Operator:
> New node created: (hostname, IP)
> New node removed successfully: yes/no
> Any issues:
