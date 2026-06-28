---
id: '004'
title: Nodes tab template and JS polling
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
depends-on:
- '003'
github-issue: ''
issue: admin-nodes-tab-manual-swarm-node-management.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# 004 — Nodes tab template and JS polling

## Description

Create the `admin/nodes.html` Jinja2 template and add a "Nodes" nav entry to
`admin/base.html`. The template provides the full Nodes admin tab UI: a node
table with Start and Remove controls, and a live Operations panel that polls
`/admin/nodes/op/<id>/status` every 2 seconds.

This ticket is UI-only. All server-side logic is in ticket 003.

### Template structure

`nodes.html` extends `admin/base.html` and follows the conventions of
`code_hosts.html` (table + inline form-POST + flash messages):

**Section 1 — Start a node**
One Bootstrap button per tier (from `tiers` context var). Each is a mini form:
```html
<form action="{{ url_for('admin.start_node') }}" method="post" style="display:inline">
  <input type="hidden" name="tier" value="{{ tier.name }}">
  <button type="submit" class="btn btn-primary">Start {{ tier.name }} (capacity {{ tier.capacity }})</button>
</form>
```

**Section 2 — Node table**
Columns: Name, IP, Role, Tier, Capacity, Hosts, State, Actions.
- Manager/leader rows: Actions cell shows "—" (no Remove button).
- Worker rows: Remove button wrapped in a `<form>` POST to
  `/admin/nodes/remove` with `fqdn` hidden field; JS `confirm()` on click.
```html
<form action="{{ url_for('admin.remove_node') }}" method="post" style="display:inline"
      onsubmit="return confirm('Remove {{ row.hostname }}? This will drain and destroy the droplet.')">
  <input type="hidden" name="fqdn" value="{{ row.hostname }}">
  <button type="submit" class="btn btn-sm btn-danger">Remove</button>
</form>
```

**Section 3 — Operations panel**
Lists `recent_ops` (from context). For each op that is `pending` or `running`,
start a JS polling loop (fetch every 2 s). When the op transitions to `done` or
`failed`, stop polling and reload the page (so the node table refreshes).

Polling pattern (adapted from `polling_script.html`):
```javascript
function pollOp(opId) {
    const url = `/admin/nodes/op/${opId}/status`;
    function tick() {
        fetch(url)
            .then(r => r.json())
            .then(data => {
                document.getElementById(`op-status-${opId}`).textContent = data.status;
                document.getElementById(`op-log-${opId}`).textContent = data.log_tail;
                if (data.status === 'done' || data.status === 'failed') {
                    location.reload();
                } else {
                    setTimeout(tick, 2000);
                }
            })
            .catch(() => setTimeout(tick, 5000));
    }
    tick();
}
```

Start `pollOp(op.id)` inline for each active op via a `<script>` block at the
bottom of the template.

Each op row in the panel shows: kind, tier or target FQDN, status badge,
created_at, a live log tail `<pre>` that updates on each poll, and a "Full log"
link to `/admin/nodes/op/<id>/log`.

## Acceptance Criteria

- [x] A "Nodes" nav link appears in the admin subnav (`admin/base.html`) pointing
      to `url_for('admin.list_nodes')`.
- [x] `admin/nodes.html` exists and extends `admin/base.html`.
- [x] Start buttons render for each tier in the `tiers` context variable.
- [x] Node table renders with correct columns for each row in `node_rows`.
- [x] Manager/leader rows have no Remove button; worker rows have a Remove button.
- [x] Remove button shows a JS `confirm()` dialog before submitting.
- [x] Operations panel lists `recent_ops` rows.
- [x] For in-progress ops, JS `pollOp()` is called automatically on page load.
- [x] Polling stops and the page reloads when an op reaches `done` or `failed`.
- [x] Full log link renders for each op (`/admin/nodes/op/<id>/log`).
- [x] Flash messages from the base template render correctly (inherited).
- [x] Template renders without error when `node_rows=[]` (Docker unreachable case).

## Implementation Plan

### Files to create
- `cspawn/admin/templates/admin/nodes.html`

### Files to modify
- `cspawn/admin/templates/admin/base.html` — add "Nodes" nav entry in the
  `<ul class="navbar-nav">` block.

### Approach

**`base.html` change**: Add one `<li>` after the "Code Hosts" entry:
```html
<li class="nav-item">
    <a class="nav-link" href="{{ url_for('admin.list_nodes') }}">Nodes</a>
</li>
```

**`nodes.html` structure**:
```
{% extends "admin/base.html" %}
{% block title %}Nodes{% endblock %}
{% block content %}
  <div class="container mt-4">
    <h1>Swarm Nodes</h1>

    {# Flash messages #}
    {% with messages = get_flashed_messages(with_categories=true) %}...{% endwith %}

    {# Start a node #}
    <div class="card mb-4">
      <div class="card-body">
        <h5 class="card-title">Start a node</h5>
        {% for tier in tiers %}
          <form action="..." method="post" style="display:inline">
            <input type="hidden" name="tier" value="{{ tier.name }}">
            <button ...>Start {{ tier.name }} (cap: {{ tier.capacity }})</button>
          </form>
        {% endfor %}
      </div>
    </div>

    {# Node table #}
    <table class="table table-striped">
      <thead><tr><th>Name</th>...</tr></thead>
      <tbody>
        {% for row in node_rows %}
        <tr>
          <td>{{ row.hostname }}</td>
          ...
          <td>
            {% if not row.is_manager and not row.is_leader %}
              <form action="..." method="post" onsubmit="return confirm(...)">
                <input type="hidden" name="fqdn" value="{{ row.hostname }}">
                <button ...>Remove</button>
              </form>
            {% else %}
              <span class="text-muted">—</span>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>

    {# Operations panel #}
    <div class="card mt-4">
      <div class="card-body">
        <h5>Operations</h5>
        {% for op in recent_ops %}
        <div id="op-row-{{ op.id }}" class="mb-2">
          <strong>{{ op.kind }}</strong>
          {{ op.tier or op.target_fqdn }}
          <span id="op-status-{{ op.id }}" class="badge ...">{{ op.status }}</span>
          <small>{{ op.created_at }}</small>
          <pre id="op-log-{{ op.id }}" class="small">{{ '' }}</pre>
          <a href="/admin/nodes/op/{{ op.id }}/log">Full log</a>
        </div>
        {% endfor %}
      </div>
    </div>
  </div>

  <script>
    function pollOp(opId) { ... }
    {% for op in recent_ops %}
      {% if op.status in ('pending', 'running') %}
        pollOp("{{ op.id }}");
      {% endif %}
    {% endfor %}
  </script>
{% endblock %}
```

### Testing plan

- Manual smoke test: load `/admin/nodes` with real Docker connected; verify table
  renders with at least one node, Start buttons appear, Remove button absent for
  manager.
- Start a node via the Start button; verify Operations panel shows the op and
  polling begins; verify page reloads when the op completes.
- Unit test (Flask test client): render `nodes.html` with mocked context variables;
  assert `<table>` present; assert Start button form `action` URL correct; assert
  manager row has no Remove form.

Run: `uv run pytest tests/test_admin_nodes_routes.py` (template rendering is tested
via the route test in ticket 003).

### Documentation updates

None required.
