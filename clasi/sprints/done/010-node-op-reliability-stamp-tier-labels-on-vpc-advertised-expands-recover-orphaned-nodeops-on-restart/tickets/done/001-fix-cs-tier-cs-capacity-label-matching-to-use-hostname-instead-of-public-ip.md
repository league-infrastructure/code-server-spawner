---
id: '001'
title: Fix cs.tier/cs.capacity label matching to use hostname instead of public IP
status: done
use-cases:
- SUC-001
- SUC-002
depends-on: []
github-issue: ''
issue: expand-tier-labels-never-stamp.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Fix cs.tier/cs.capacity label matching to use hostname instead of public IP

## Description

The `cs.tier`/`cs.capacity` labeling block at the end of `_join_swarm`
(`cspawn/cli/node.py:1651-1678`) locates the just-joined node by comparing
each swarm node's `Status.Addr` to the droplet's **public** IP. Every node
joins with `--advertise-addr <10.124.x.x>` (the VPC address, resolved as
`worker_vpc_ip` at `node.py:1548` and used in the join command at
`node.py:1602-1606`), so Docker Swarm always reports `Status.Addr` as the
**private** VPC address — the comparison can never succeed. The loop spins
its full 90s deadline, then falls through a bare `except: pass`: no log, no
error. Every VPC-advertised expand (i.e. every expand in every deployment
today) leaves `cs.tier`/`cs.capacity` unset; the admin Nodes tab shows
`---`; `tiers.py::node_capacity()` falls back to `DEFAULT_CAPACITY` (6),
under-counting a large-tier node (true capacity 14) in the autoscaler until
an operator manually runs `node label-backfill --apply`. Confirmed live
twice on 2026-07-06 (swarm3, swarm4).

The `code-host-user`/`SWARM_NODE_LABEL` block immediately above
(`node.py:1618-1649`) has the identical underlying IP-matching defect, but
survives it via a name-guess fallback that fires once its own 90s deadline
expires — that fallback, not the IP-matching loop, is why
`code-host-user=true` reliably gets applied today. The cs.tier block has no
such fallback.

**Fix:** replace the IP-matching loop with a new
`_apply_labels_after_join(manager_client, target, labels, *,
deadline_seconds=90.0, poll_interval=3.0, log=None) -> bool` helper that
polls `manager_client.nodes.list()` for a node matching `target`'s hostname
or short name — via this file's existing `_find_swarm_node`, already
proven by drain and `label-backfill` — and applies `labels` via the
existing `_ensure_node_labels` once found. If the deadline passes with no
match, log a `WARNING` naming `target` and the label keys that were not
applied, and return `False`. Never raise.

**Explicitly out of scope for this ticket:** the `code-host-user`/
`SWARM_NODE_LABEL` block is left untouched. It is not reported broken
(its fallback already works), and refactoring it broadens this ticket's
blast radius into a working code path for no required behavior change. See
`architecture-update.md` Step 6 ("Decision: Do not refactor the
code-host-user/SWARM_NODE_LABEL block in this sprint") and Step 7, Open
Question 1, for the deferred-cleanup rationale.

See `clasi/issues/expand-tier-labels-never-stamp.md` for the full
root-cause diagnosis and this sprint's `architecture-update.md` Steps 1, 3
(M1), 5, and 6 for the detailed design and rationale.

## Acceptance Criteria

- [x] New `_apply_labels_after_join(manager_client, target, labels, *,
  deadline_seconds=90.0, poll_interval=3.0, log=None) -> bool` added to
  `cspawn/cli/node.py`, matching the joined node by hostname/short-name via
  `_find_swarm_node` — never by comparing `Status.Addr` to a public IP.
- [x] `_join_swarm`'s cs.tier/cs.capacity block (`node.py:1651-1678`) is
  replaced by a call to this helper: `{"cs.tier": tier.name, "cs.capacity":
  str(tier.capacity)}` when `tier is not None`.
- [x] A full `node expand --tier <t>` exercised with a mocked manager client
  whose `Status.Addr` is a private VPC address different from the
  droplet's public IP (mirroring the live-confirmed scenario) results in
  `cs.tier`/`cs.capacity` being written via the mocked `update_node` call.
- [x] When the target node never appears in `manager_client.nodes.list()`
  within the deadline, a `WARNING` is logged naming the node and the label
  keys that were not applied; the function returns `False` and does not
  raise.
- [x] The `code-host-user`/`SWARM_NODE_LABEL` block (`node.py:1618-1649`) is
  unchanged — verified by running the existing test suite with no
  modifications needed to any test covering that block's behavior.
- [x] `node label-backfill`'s `tier_for_slug`-based mapping and
  `_ensure_node_labels` are unchanged and reused as-is.

## Implementation Plan

**Approach**: Extract the join-time label-application logic into one small,
independently testable helper. Replace only the cs.tier/cs.capacity block's
body with a call to it; leave every other block in `_join_swarm` untouched.
The helper takes `target` (the same string already passed into
`_join_swarm`, which is guaranteed to be a real hostname — not a bare IP —
in every code path where `tier` is non-`None`; see
`architecture-update.md` Step 1 for why) rather than an IP, and computes
its own short-name guess (`target.split(".")[0]`) to pass alongside
`target` into `_find_swarm_node`.

**Files to create/modify**:
- `cspawn/cli/node.py` — add `_apply_labels_after_join`; replace the
  cs.tier/cs.capacity block inside `_join_swarm` (`node.py:1651-1678`) with
  a call to it.
- `test/test_node_labels.py` — extend with new test cases (see below).

**Testing plan**:
- Follow `test/test_node_labels.py`'s existing `_make_manager_client(...)`
  MagicMock helper pattern (mocked `client.nodes.list()`,
  `client.api.inspect_node()`, `client.api.update_node()`).
- New test: manager client's `inspect_node` reports `Status.Addr` as a
  private VPC address (e.g. `10.124.0.6`) while the droplet's tracked
  public IP differs (e.g. `164.92.116.173`) — assert
  `_apply_labels_after_join` finds the node by hostname and
  `client.api.update_node` is called with `cs.tier`/`cs.capacity` in the
  updated spec.
- New test: node never appears in `client.nodes.list()` (or its hostname
  never matches) — assert the function returns `False`, `update_node` is
  never called, and a `WARNING`-level log record is emitted naming the
  target (use `caplog`, matching the existing
  `test_ensure_node_labels_logs_warning_on_error` pattern in the same
  file).
- New test: node found immediately (first poll iteration) — assert no
  unnecessary sleep/delay (can patch `time.sleep` to assert it's never
  called in the immediate-match case).
- Run the full existing `test/test_node_labels.py` suite unmodified to
  confirm no regression to `_ensure_node_labels`/tier-resolution tests.

**Documentation updates**: Update `_join_swarm`'s docstring to note that
tier labels are applied via hostname matching (not IP), and reference the
new helper.

## Testing

- **Existing tests to run**: `uv run pytest test/test_node_labels.py`
- **New tests to write**: extend `test/test_node_labels.py` with the three
  cases above (VPC-mismatched-IP success, timeout-logs-warning, immediate
  match with no sleep).
- **Verification command**: `uv run pytest`
