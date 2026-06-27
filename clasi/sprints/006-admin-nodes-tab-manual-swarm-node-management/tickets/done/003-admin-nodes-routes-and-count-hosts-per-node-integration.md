---
id: '003'
title: Admin nodes routes and count_hosts_per_node integration
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
depends-on:
- '001'
- '002'
github-issue: ''
issue: ''
completes_issue: false
---

# Admin nodes routes and count_hosts_per_node integration

## Description

Add five new routes to `cspawn/admin/routes.py`, all decorated with `@admin_required`, covering the full server-side logic for the Nodes admin tab:

1. `GET /admin/nodes` — list all swarm nodes with host counts, tiers, and recent ops.
2. `POST /admin/nodes/start` — validate tier, create `NodeOp(kind='expand')`, launch detached `cspawnctl node op-run` subprocess.
3. `POST /admin/nodes/remove` — refuse manager/leader, create `NodeOp(kind='remove')`, launch detached subprocess.
4. `GET /admin/nodes/op/<id>/status` — JSON poll endpoint: `{status, exit_code, message, log_tail}`.
5. `GET /admin/nodes/op/<id>/log` — full plain-text op log.

This ticket adds all backend logic. The template (ticket 004) builds on these routes.

## Acceptance Criteria

- [x] `GET /admin/nodes` returns HTTP 200 with a node list when the Docker client mock returns nodes and tasks; renders `admin/nodes.html`.
- [x] `GET /admin/nodes` passes `node_rows` (list of dicts with keys: `hostname`, `ip`, `role`, `tier`, `capacity`, `host_count`, `availability`), `tiers` (from `load_tiers`), and `recent_ops` (last 20 `NodeOp` rows ordered by `created_at` desc) to the template context.
- [x] `POST /admin/nodes/start` with a valid `tier` creates a `NodeOp` row with `kind='expand'`, `tier=<name>`, `status='pending'`; calls `subprocess.Popen` with the `cspawnctl` command; flashes a success message; redirects to `/admin/nodes`.
- [x] `POST /admin/nodes/start` with an invalid `tier` flashes an error and redirects without creating a `NodeOp` or calling `Popen`.
- [x] `POST /admin/nodes/remove` with a worker node FQDN creates a `NodeOp(kind='remove')`; calls `Popen`; flashes success; redirects.
- [x] `POST /admin/nodes/remove` with a manager or leader node FQDN is refused: flashes an error, no `NodeOp` created, no `Popen` called.
- [x] `GET /admin/nodes/op/<id>/status` returns JSON `{status, exit_code, message, log_tail}` for a known op; `log_tail` is the last 50 lines of the log file (empty string if file absent).
- [x] `GET /admin/nodes/op/<id>/status` returns 404 for an unknown op ID.
- [x] `GET /admin/nodes/op/<id>/log` returns plain text of the full log file; 404 if op not found.
- [x] All routes return 302 redirect to the main index (not 200) when accessed by a non-admin user (existing `admin_required` behavior).

## Implementation Plan

### Approach

Add imports and five route functions to `cspawn/admin/routes.py`. Follow the exact style of existing routes:
- `@admin_bp.route(...)` decorator
- `@admin_required` decorator
- `flash(...)` + `redirect(url_for(...))` for POST routes
- `render_template(...)` for GET routes

**`GET /admin/nodes` (`list_nodes`):**
```python
@admin_bp.route("/nodes")
@admin_required
def list_nodes():
    from cspawn.cli.node import count_hosts_per_node
    from cspawn.cs_docker.tiers import load_tiers
    cfg = ca.app_config
    docker_uri = cfg.get("DOCKER_URI")
    node_rows = []
    try:
        client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
        host_counts = count_hosts_per_node(client)
        for n in client.nodes.list():
            spec = n.attrs.get("Spec", {})
            desc = n.attrs.get("Description", {})
            status = n.attrs.get("Status", {})
            ms = n.attrs.get("ManagerStatus") or {}
            labels = spec.get("Labels") or {}
            hostname = desc.get("Hostname", "")
            role = (spec.get("Role") or "worker").lower()
            is_leader = bool(ms.get("Leader"))
            short = hostname.split(".")[0]
            node_rows.append({
                "hostname": hostname,
                "short": short,
                "ip": status.get("Addr", ""),
                "role": "leader" if is_leader else role,
                "tier": labels.get("cs.tier", ""),
                "capacity": labels.get("cs.capacity", ""),
                "host_count": host_counts.get(short, 0),
                "availability": spec.get("Availability", ""),
                "is_manager": role == "manager",
                "is_leader": is_leader,
            })
        client.close()
    except Exception as e:
        flash(f"Could not connect to Docker: {e}", "danger")
    tiers = load_tiers(ca.app_config)
    recent_ops = NodeOp.query.order_by(NodeOp.created_at.desc()).limit(20).all()
    return render_template("admin/nodes.html", node_rows=node_rows, tiers=tiers, recent_ops=recent_ops)
```

**`POST /admin/nodes/start`:**
- Validate `tier` field against `load_tiers(cfg)` names.
- Create `NodeOp(id=str(uuid4()), kind='expand', tier=tier_name, status='pending', log_path=..., created_by=current_user.id, created_at=datetime.utcnow())`.
- `db.session.add(op); db.session.commit()`.
- `subprocess.Popen(["cspawnctl", "-d", deploy, "node", "op-run", str(op.id)], start_new_session=True, stdout=DEVNULL, stderr=DEVNULL)`.
- `flash(f"Starting node (op {op.id})", "success"); return redirect(url_for("admin.list_nodes"))`.

**`POST /admin/nodes/remove`:**
- Get `fqdn` from form.
- Re-query Docker to confirm node is not manager/leader (or check from a passed fqdn vs node list; simpler: re-query). If manager/leader: flash error, redirect.
- Create `NodeOp(kind='remove', target_fqdn=fqdn, ...)` and launch subprocess as above.

**`GET /admin/nodes/op/<id>/status`:**
- `op = NodeOp.query.get(id) or abort(404)`.
- Read log tail: open `op.log_path`, seek to last N bytes, return last 50 lines (or empty string if file absent/unreadable).
- Return `jsonify({status: op.status, exit_code: op.exit_code, message: op.message, log_tail: tail})`.

**`GET /admin/nodes/op/<id>/log`:**
- `op = NodeOp.query.get(id) or abort(404)`.
- Read full log file or return empty string.
- Return `Response(log_text, mimetype="text/plain")`.

**Import additions** at top of `routes.py`:
- `import subprocess, sys, uuid, docker` (docker may already be available as a dep).
- `from datetime import datetime, timezone`.
- `from cspawn.models import ..., NodeOp` (add `NodeOp` to existing models import).

The `deploy` name comes from `ca.app_config.get("JTL_DEPLOYMENT", "devel")`.

### Files to modify

- `cspawn/admin/routes.py` — add imports and five route functions.

### Testing plan

- Unit test: `GET /admin/nodes` — mock `docker.DockerClient`, `count_hosts_per_node`, `load_tiers`; assert 200, `node_rows` in context, `tiers` in context.
- Unit test: `POST /admin/nodes/start` valid tier — mock `subprocess.Popen`; assert `NodeOp` row created with correct fields; `Popen` called with correct args; response is 302 to list.
- Unit test: `POST /admin/nodes/start` invalid tier — assert no `NodeOp` created, no `Popen` call, flash error.
- Unit test: `POST /admin/nodes/remove` worker node — mock Docker + Popen; assert `NodeOp(kind='remove')` created; Popen called.
- Unit test: `POST /admin/nodes/remove` manager node — assert refused (flash error, no NodeOp, no Popen).
- Unit test: `GET /admin/nodes/op/<id>/status` — create `NodeOp` in DB; assert JSON response fields; assert log_tail from a temp log file.
- Unit test: `GET /admin/nodes/op/<id>/status` unknown id — assert 404.
- Unit test: `GET /admin/nodes/op/<id>/log` — assert plain text response.
- Unit test: non-admin access to all routes — assert 302 redirect to main.index (existing `admin_required` behavior).
- Run `uv run pytest` for full regression check.

### Documentation updates

None required. Route behavior is described in `architecture-update.md`.
