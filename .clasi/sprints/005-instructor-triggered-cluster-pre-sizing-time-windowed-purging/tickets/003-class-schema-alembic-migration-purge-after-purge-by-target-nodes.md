---
id: '003'
title: "Class detail page — Create my cluster button and status display"
status: open
use-cases:
  - SUC-001
  - SUC-005
depends-on:
  - '002'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Class detail page — Create my cluster button and status display

## Description

Add a "Create my cluster" section to the class detail page at
`cspawn/main/templates/classes/detail.html`. This section is only visible to
instructors of the class. It:

1. Shows a "Create my cluster" button when no purge window is armed (or after
   it has expired).
2. POSTs to `POST /classes/<id>/cluster` (from ticket 002) on button click, then
   updates the status display from the JSON response — no page reload required.
3. Shows the current cluster zone status (`provisioning`, `active`, `expired`)
   when a window is set.

**Inert-by-default:** The button only calls the stamping route (ticket 002) which
never provisions inline. No Docker/DO calls occur from the UI.

### Zone display

| Zone | Display |
|---|---|
| `unarmed` | "Create my cluster" button (enabled) |
| `provisioning` | "Cluster pre-sized — N nodes requested. Window opens at [purge_after]." + re-arm button |
| `active` | "Cluster active — idle reclaim enabled. Expires at [purge_by]." + re-arm button |
| `expired` | "Cluster window expired." + "Create new cluster" button |

### JavaScript pattern

Follow the same `fetch`-based pattern used elsewhere in the detail template.
On button click:
1. `fetch('/classes/<id>/cluster', {method: 'POST'})` — no page reload.
2. On success (200 JSON), immediately `fetch('/classes/<id>/cluster/status')`
   and update the status `<div>` from the response.

## Acceptance Criteria

- [ ] The class detail page shows the cluster section only to instructors of
      the class (not to students, not to other instructors not on the class).
- [ ] The "Create my cluster" button is present when `class.purge_after` is
      None (unarmed) or `now >= class.purge_by` (expired).
- [ ] Clicking the button POSTs to `/classes/<id>/cluster` and updates the
      status display from the JSON response without a page reload.
- [ ] Status text reflects the four zone states correctly.
- [ ] `purge_after` and `purge_by` are shown in a human-readable format
      (e.g. "6:00 PM" using the class timezone or UTC).
- [ ] `target_nodes` is displayed in the provisioning/active status (e.g. "3
      nodes requested").
- [ ] The button is disabled (or removed) while the request is in-flight to
      prevent double-submission.
- [ ] The cluster section is absent entirely when the viewing user is not an
      instructor for this class.

## Implementation Plan

**Files to modify:**
- `cspawn/main/templates/classes/detail.html` — add a cluster status section
  after the existing class control buttons. Use `{% if current_user in class_.instructors %}` guard.

**No new Python files needed** — this ticket is template + JS only. Routes
were added in ticket 002.

**Testing plan:**
- Manual visual verification using the Flask dev server (no DO needed).
- Write a Flask test client test that:
  1. Renders the class detail page as an instructor — asserts the cluster section
     is present.
  2. Renders the class detail page as a student — asserts the cluster section
     is absent.
- No JS unit tests required; fetch pattern is identical to existing code.

## Verification Command

```
uv run pytest tests/ -k "detail" -v
# Then visually verify in browser with Flask dev server
```
