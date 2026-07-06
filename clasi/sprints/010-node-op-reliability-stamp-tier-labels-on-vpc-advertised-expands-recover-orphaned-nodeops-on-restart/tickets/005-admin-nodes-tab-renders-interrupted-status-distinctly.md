---
id: '005'
title: Admin Nodes tab renders interrupted status distinctly
status: open
use-cases:
- SUC-005
depends-on:
- '003'
- '004'
github-issue: ''
issue: nodeop-orphaned-on-container-restart.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Admin Nodes tab renders interrupted status distinctly

## Description

Tickets 002-004 make `interrupted` a real, populated `NodeOp` status, but
`cspawn/admin/templates/admin/nodes.html`'s status badge (lines ~139-144)
and its JS mirror (`pollOp()`, lines ~176-185) only know about `done`
(green), `failed` (red), and `running` (yellow) — anything else, including
`pending` today and `interrupted` after this sprint, falls into a generic
`bg-secondary` "else" branch. An operator scanning the Nodes tab after a
restart would see an `interrupted` op rendered identically to a normal
`pending` op — no visual signal that this one needs attention. The op's
`message` field (which, after ticket 003, may name an orphaned droplet) is
also not rendered anywhere in the table today — only reachable via the raw
per-op log file or the JSON status endpoint, neither of which surfaces it
proactively.

**Fix:**
1. Add an `interrupted` branch to the status badge's Jinja conditional
   (distinct from `bg-secondary`, e.g. `bg-dark text-white`), and the
   matching branch in the JS color-mapping `if/else` chain in `pollOp()`
   (kept in sync for consistency, even though `interrupted` rows are never
   actually polled — see next point).
2. Add a `title="{{ op.message or '' }}"` attribute to the status badge
   span so the orphan note (or any other message) is visible on hover
   without opening the full log.
3. No change needed to confirm `interrupted` is excluded from
   polling — it already is, structurally: `pollOp(...)` is only invoked
   for rows where `{% if op.status in ('pending', 'running') %}` at
   render time (`nodes.html` line ~202), and `interrupted` is never in
   that set. This ticket's tests should assert that exclusion explicitly
   as a regression guard, since it's easy to accidentally break if that
   condition is ever touched.

No changes needed to `cspawn/admin/routes.py` — `op.status`/`op.message`
already flow through `list_nodes`/`node_op_status` generically; `interrupted`
is simply a new value for an already-generic field.

See `clasi/issues/nodeop-orphaned-on-container-restart.md` (acceptance
criterion: "Admin UI renders `interrupted` distinctly (not spinning)") and
`architecture-update.md` Step 3 (M4) and Step 5 for the full design.

## Acceptance Criteria

- [ ] The status badge Jinja block in `admin/templates/admin/nodes.html`
  gains an `{%- elif op.status == 'interrupted' %}` branch with a badge
  class distinct from `bg-success` (done), `bg-danger` (failed),
  `bg-warning text-dark` (running), and `bg-secondary` (pending/unknown) —
  e.g. `bg-dark text-white`.
- [ ] The status badge span carries `title="{{ op.message or '' }}"` so the
  message (including any orphan-droplet note from ticket 003) is visible
  as a browser tooltip.
- [ ] The `pollOp()` JavaScript's color-mapping `if/else` chain gains the
  matching `interrupted` → dark branch, for consistency with the
  server-rendered initial state.
- [ ] A rendered page containing an `interrupted` op does *not* call
  `pollOp(...)` for that op's id (regression guard confirming the existing
  `{% if op.status in ('pending', 'running') %}` gate still excludes it).
- [ ] A rendered page containing an `interrupted` op with a non-empty
  `message` renders that message text somewhere in the page output (via
  the `title` attribute or equivalent) — verified by asserting the message
  string appears in the rendered HTML.
- [ ] No changes to `cspawn/admin/routes.py` are required or made.

## Implementation Plan

**Approach**: Purely template-level change (Jinja + inline JS in the same
file) plus a `title` attribute for message visibility. No route or model
change. Keep the Jinja and JS color-mapping branches in the same order/style
as the existing `done`/`failed`/`running` branches for readability.

**Files to create/modify**:
- `cspawn/admin/templates/admin/nodes.html` — status badge Jinja block;
  `pollOp()` JS color-mapping; `title` attribute on the badge span.
- `test/test_admin_nodes_routes.py` and/or
  `test/test_admin_nodes_template.py` — extend with new test cases (see
  below; use whichever of the two already covers template-rendering
  assertions for the ops table, per that file's existing conventions).

**Testing plan**:
- Follow this repo's existing template-testing conventions (rendering the
  `admin/nodes.html` template with a Flask test client / test request
  context and a seeded `NodeOp` row, per `test_admin_nodes_routes.py`'s
  `TestListNodes`/`client` fixture pattern, or `test_admin_nodes_template.py`
  if that file renders the template more directly).
- New test: a `NodeOp` with `status='interrupted'` renders with the new
  badge class in the response HTML (assert the class string appears
  associated with that op's row/id).
- New test: a `NodeOp` with `status='interrupted'` and a non-empty
  `message` (e.g. containing a droplet fqdn) renders that message text in
  the response HTML.
- New test: confirm the rendered page's inline `<script>` block does not
  contain a `pollOp("<id>")` call for an `interrupted` op's id, while it
  does for a `pending`/`running` op's id in the same response (string
  containment assertions against the rendered HTML, matching this
  template's existing testability — no headless browser needed).
- Run the full existing `test/test_admin_nodes_routes.py` and
  `test/test_admin_nodes_template.py` suites to confirm no regression to
  `done`/`failed`/`running`/`pending` rendering.

**Documentation updates**: None beyond an inline HTML comment noting why
`interrupted` gets its own badge color (cross-reference the issue/sprint).

## Testing

- **Existing tests to run**: `uv run pytest test/test_admin_nodes_routes.py
  test/test_admin_nodes_template.py`
- **New tests to write**: extend one or both of those files with the three
  cases above (badge class, message visibility, poll-exclusion regression
  guard).
- **Verification command**: `uv run pytest`
