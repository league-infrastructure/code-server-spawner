---
status: pending
---

# Admin "Nodes" tab — manual swarm node management

## Context

The autoscaler (sprints 003–005) ships **disabled by default** and its pre-sizing
demand logic is wrong (it gates provisioning on the *reaping* window, so an
armed class isn't pre-sized during class — see `estimate_demand` in
`cspawn/cs_docker/autoscale.py`). Rather than fix/enable the autoscaler now, the
stakeholder wants **direct manual control**: an admin web tab to list swarm nodes
with their code-server host counts, start a large/small node immediately, and
drain+destroy a node — reusing the existing `cspawnctl node expand --tier` /
`node stop` machinery (which already handles swarm-join + DNS).

Decisions (stakeholder): **detached `cspawnctl` subprocess** (start immediately,
no request blocking); **live status + per-op log**; **must work in the deployed
prod spawner**, not just the local app.

The autoscaler is left in place but stays inert; this tab is the manual
alternative. No change to the autoscaler is required.

## Approach

A node operation (start/remove) takes ~1–2 min, so the web route records a job,
launches a detached `cspawnctl node op-run <id>` subprocess that does the work
(reusing the existing `expand`/`stop_node` commands) and tees its output to a log
file, and returns instantly. The UI lists nodes (read-only docker query) and
polls the job for live status — reusing the existing host-readiness polling
pattern (`cspawn/main/templates/elements/polling_script.html` + `/host/is_ready`).

### 1. Job model — `NodeOp` (`cspawn/models.py` + migration)
New table tracking each operation:
`id, kind ('expand'|'remove'), tier (nullable), target_fqdn (nullable),
status ('pending'|'running'|'done'|'failed'), exit_code, log_path, message,
created_by (user_id FK), created_at, started_at, finished_at`.
Add a hand-written **idempotent Alembic migration** under
`migrations/versions/` (same approach as sprint 005's
`v001_add_class_purge_window_fields.py`: `CREATE TABLE IF NOT EXISTS` for
Postgres, batch op for SQLite tests; `db.create_all()` won't create it in prod).

### 2. CLI worker — `cspawnctl node op-run <op_id>` (`cspawn/cli/node.py`)
The detached subprocess target. It:
- loads the `NodeOp`, sets `status=running`/`started_at`;
- acquires an `fcntl.flock` on `{DATA_DIR}/.node-ops.lock` (serialize ops — avoids
  `_get_next_serial` races on concurrent expands; reuse the lock idiom from
  `run_autoscale` in `autoscale.py`);
- redirects stdout/stderr to `log_path` (`{DATA_DIR}/node-ops/<id>.log`);
- **kind=expand:** `ctx.invoke(expand, tier_name=<tier>, ...)` — reuses the exact
  `expand` command ([node.py:1854](cspawn/cli/node.py#L1854)) → create droplet +
  configure + join swarm + `_sync_domain_records` (DNS) all happen as today;
- **kind=remove:** `ctx.invoke(stop_node, node_spec=<fqdn>, force=False, dry_run=False)`
  ([node.py:1797](cspawn/cli/node.py#L1797)) → `graceful_remove_node` drain → wait
  → remove-from-swarm → destroy droplet;
- sets `status=done|failed`, `exit_code`, `message`, `finished_at`.

This is mostly orchestration — the real work is the already-tested commands.

### 3. Admin routes (`cspawn/admin/routes.py`, all `@admin_required`)
- `GET /admin/nodes` (`list_nodes`): build rows from a **fresh** read-only
  `docker.DockerClient(base_url=DOCKER_URI, use_ssh_client=True)` (per-request; do
  not reuse `app.csm`'s cached client) — `count_hosts_per_node(client)`
  ([node.py:53](cspawn/cli/node.py#L53)) + `client.nodes.list()` → per node:
  hostname, IP (`Status.Addr`), is_manager/leader, tier (`cs.tier` label),
  capacity (`cs.capacity` label), host_count, availability. Pass `load_tiers(cfg)`
  (`cspawn/cs_docker/tiers.py`) for the Start buttons + running/recent `NodeOp`s.
- `POST /admin/nodes/start` (field `tier`): create `NodeOp(kind=expand, tier)`,
  `subprocess.Popen(["cspawnctl","-d",<deploy>,"node","op-run",str(op.id)],
  start_new_session=True, stdout=DEVNULL, stderr=DEVNULL)`, flash, redirect. Deploy
  name from app config / `JTL_DEPLOYMENT`.
- `POST /admin/nodes/remove` (field `fqdn`): refuse manager/leader; create
  `NodeOp(kind=remove, target=fqdn)`; launch op-run detached; flash; redirect.
- `GET /admin/nodes/op/<id>/status`: JSON `{status, exit_code, message, log_tail}`
  (last ~50 lines) for polling.
- `GET /admin/nodes/op/<id>/log`: full log text (optional view).

### 4. Templates
- Add a **Nodes** tab to the nav in
  `cspawn/admin/templates/admin/base.html` (the `<ul class="navbar-nav">` block).
- New `cspawn/admin/templates/admin/nodes.html` (extends `admin/base.html`,
  follows `code_hosts.html` table + inline-form-POST + flash conventions):
  - "Start a node": **Start large** / **Start small** buttons (from tiers) → POST
    `/admin/nodes/start`.
  - Node table: Name, IP, Role, Tier, Capacity, **Hosts** (count), State, Actions —
    a **Remove** button per worker (JS `confirm()`; disabled for the manager).
  - "Operations" panel: running/recent `NodeOp`s with **live status** polled from
    `/admin/nodes/op/<id>/status` every 2s (mirror `polling_script.html`), each with
    a log link; refresh the node table when an op finishes.

### 5. Prod deployment prerequisite (verified gap)
The `codeserver_codeserver` container has `cspawnctl`, `DO_TOKEN`, `DOCKER_URI`,
`DATA_DIR=/app/data`, `JTL_DEPLOYMENT=prod`, and `cspawnctl node info` works — but
`_ensure_priv_key()` ([node.py:765](cspawn/cli/node.py#L765)) needs
`config/secrets/id_rsa(.pub)`, which is **absent** (`/app/config/secrets/` empty;
the working swarm1 key lives at `/root/.ssh/id_rsa`). Fix: make `_ensure_priv_key()`
**fall back to `~/.ssh/id_rsa`** when `config/secrets/id_rsa` is missing (small,
robust), and confirm that container key's public half is a registered DO SSH key
so new droplets accept it. Without this, expand-from-prod fails at the new-droplet
SSH step. (Local-prod already has `config/secrets/id_rsa`, so the local app works
unchanged.)

## Critical files
- `cspawn/models.py` — `NodeOp` model; `migrations/versions/<new>.py` — migration.
- `cspawn/cli/node.py` — new `node op-run` command; `_ensure_priv_key()` fallback.
  Reuses `expand`, `stop_node`, `count_hosts_per_node`, `graceful_remove_node`,
  `_sync_domain_records`.
- `cspawn/cs_docker/tiers.py` — `load_tiers`/`tier_by_name` (reuse, no change).
- `cspawn/admin/routes.py` — 5 routes + a `list_nodes` helper.
- `cspawn/admin/templates/admin/base.html` — nav tab.
- `cspawn/admin/templates/admin/nodes.html` — new template (+ poll script).

## Verification
- **Unit tests**: `NodeOp` model + migration apply (Postgres `IF NOT EXISTS` /
  SQLite batch, per sprint-005 test); `list_nodes` row-building with a mocked
  docker client; `node op-run` status transitions (mock `ctx.invoke`, assert
  done/failed + flock held); route auth (`@admin_required` rejects non-admins) and
  that `POST start/remove` insert a `NodeOp` and call `subprocess.Popen` (mock
  Popen); remove refuses the manager.
- **Manual e2e (local-prod)**: `/admin/nodes` → "Start small node" → op shows
  `running`, log streams; ~90s later a new `swarmN` row appears + its DNS A record
  is created (`node info` / DO DNS). Then "Remove" it → drains + destroys; it
  disappears and the stale DNS reconciles on the next expand. Watch the op log.
- **Prod**: after the `_ensure_priv_key` fallback ships and the container is
  redeployed, repeat e2e in the deployed app.

## Notes / scope
- Substantial enough for a CLASI sprint (model + migration + CLI command + 5
  routes + template + tests + the prod key prerequisite + a deploy).
- `_sync_domain_records` is invoked by `expand` already, so new nodes get DNS and
  the reconcile cleans stale records — no extra DNS code needed.
- The detached subprocess survives gunicorn worker recycling via
  `start_new_session=True`; logs go to a file (not pipes) so nothing blocks.
