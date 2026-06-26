---
id: "005"
title: "capacity-aware contract"
status: open
use-cases: [SUC-005]
depends-on: ["002", "003", "004"]
github-issue: ""
issue: "multi-size-node-provisioning.md"
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# capacity-aware contract

## Description

Rework `contract_node` in `cspawn/cli/node.py` (line 1823) to only remove empty nodes
and to prefer the smallest-tier, newest node as the removal candidate. The current
implementation selects the highest-serial worker with no emptiness check — it can destroy
a node with live student code-server sessions.

This ticket also extracts two reusable helpers:

- `_running_hosts_by_node(client) -> dict[str, int]`: factors out the per-node host count
  loop from the `hosts` command (line 75-83) so both commands share the same implementation.
- `_select_contract_candidate(client, cfg) -> tuple[int, str] | None`: encapsulates the
  entire selection policy so the future autoscaler can call it directly without duplicating
  the logic.

**Pre-condition for production**: run `label-backfill --apply` (ticket 004) on existing
nodes before deploying this ticket. Without labels, all nodes fall back to `DEFAULT_CAPACITY`
and the tier-sort is neutral.

**Depends on**: 002 (`node_capacity`, `default_capacity`), 003 (`_ensure_node_labels`,
labels present at join), 004 (labels present on existing nodes).

## Acceptance Criteria

- [ ] `_running_hosts_by_node(client) -> dict[str, int]` exists as a module-level function that returns `{short_name: count}` of running code-server tasks per node.
- [ ] The `hosts` command body delegates its per-node count to `_running_hosts_by_node` (behavior identical to before).
- [ ] `_select_contract_candidate(client, cfg) -> tuple[int, str] | None` exists as a module-level function.
- [ ] `_select_contract_candidate` returns `None` when no eligible worker node has zero running hosts.
- [ ] `_select_contract_candidate` selects among empty-only nodes, sorted by `(capacity ASC, serial DESC)` — smallest capacity first, newest (highest serial) as tiebreaker.
- [ ] `_select_contract_candidate` reads `cs.capacity` from node labels via `node_capacity(node_attrs, cfg)` from `tiers.py`; falls back to `default_capacity(cfg)` for unlabeled nodes.
- [ ] `contract_node` calls `_select_contract_candidate`; if `None` is returned, prints "No empty node to contract." and exits cleanly (exit code 0).
- [ ] `contract_node` does NOT remove a node with `running_hosts > 0` under any circumstance.
- [ ] `contract --dry-run` prints the selected candidate (or "No empty node") without destroying anything.
- [ ] `contract_node` docstring and help text note the behavioral change: "only removes empty nodes; use 'cspawnctl node stop <node>' to force-remove a loaded node."
- [ ] Unit tests for `_select_contract_candidate` pass (see Testing Plan).
- [ ] `uv run pytest` passes with no regressions.

## Implementation Plan

### Approach

Three changes to `cspawn/cli/node.py`, in dependency order:

1. Extract `_running_hosts_by_node` from `hosts` (line 75-83).
2. Write `_select_contract_candidate` using `_running_hosts_by_node` + node label reads.
3. Replace the selection logic in `contract_node` with a call to `_select_contract_candidate`.

### Step 1: Extract `_running_hosts_by_node`

Insert before the `hosts` command (around line 47):

```python
def _running_hosts_by_node(client: docker.DockerClient) -> dict[str, int]:
    """Return {short_node_name: running_host_count} for all swarm nodes.

    Counts running tasks for services labeled jtl.codeserver=true.
    Used by both the 'hosts' command and 'contract' candidate selection.
    """
    from collections import defaultdict
    node_name_map: dict[str, str] = {}
    for n in client.nodes.list():
        hn = n.attrs.get("Description", {}).get("Hostname", "") or n.id
        node_name_map[n.id] = hn.split(".")[0]

    per_node: dict[str, int] = defaultdict(int)
    for svc in client.services.list(filters={"label": "jtl.codeserver=true"}):
        for t in svc.tasks(filters={"desired-state": "running"}):
            if (t.get("Status", {}) or {}).get("State") != "running":
                continue
            nid = t.get("NodeID")
            short = node_name_map.get(nid, nid or "?")
            per_node[short] += 1

    return dict(per_node)
```

Update the `hosts` command body (lines 70-83) to call `_running_hosts_by_node(client)`.
The output format remains identical.

### Step 2: Write `_select_contract_candidate`

Insert before `contract_node` (around line 1823):

```python
def _select_contract_candidate(
    client: docker.DockerClient, cfg
) -> tuple[int, str] | None:
    """Return (serial, fqdn) of the best empty worker to remove, or None.

    Selection policy:
    - Eligible: hostname matches DO_NAMES, not leader, not manager, running_hosts == 0.
    - Sort key: (capacity ASC, serial DESC) — remove smallest capacity first;
      among ties, remove the newest (highest serial) to preserve long-lived nodes.

    Returns None if no eligible empty node exists. Never returns a loaded node.
    """
    from cspawn.cs_docker.tiers import node_capacity

    name_template = cfg.get("DO_NAMES")
    if not name_template:
        return None

    pat = _regex_from_template(name_template)
    running = _running_hosts_by_node(client)

    candidates = []
    for n in client.nodes.list():
        attrs = n.attrs or {}
        hostname = ((attrs.get("Description") or {}).get("Hostname") or "").strip()
        if not hostname or not pat.match(hostname):
            continue
        short = hostname.split(".")[0]
        role = ((attrs.get("Spec") or {}).get("Role") or "").lower()
        is_leader = bool(((attrs.get("ManagerStatus") or {}).get("Leader")) or False)
        if is_leader or role == "manager":
            continue
        host_count = running.get(short, 0)
        if host_count > 0:
            continue  # never remove a loaded node

        m = pat.match(hostname)
        try:
            serial = int(m.group(1))
        except Exception:
            continue

        cap = node_capacity(attrs, cfg)
        candidates.append((cap, -serial, serial, hostname))  # -serial for DESC sort

    if not candidates:
        return None

    candidates.sort()  # (capacity ASC, -serial ASC i.e. serial DESC)
    _, _, serial, fqdn = candidates[0]
    return (serial, fqdn)
```

### Step 3: Replace `contract_node` selection logic (line 1823)

Replace the entire node-scanning loop (lines 1848-1881) and the `selected` variable
with a call to `_select_contract_candidate`:

```python
@node.command(name="contract")
@click.option("-N", "--dry-run", is_flag=True, help="Only print what would be done")
@click.pass_context
def contract_node(ctx, dry_run: bool):
    """Shrink the cluster by removing the smallest empty worker node.

    Only removes nodes with zero running code-server hosts. If no empty node
    exists, exits cleanly without removing anything.

    To force-remove a loaded node, use: cspawnctl node stop <node>
    """
    log = get_logger(ctx)
    cfg = get_config()

    docker_uri = cfg.get("DOCKER_URI")
    name_template = cfg.get("DO_NAMES")
    if not (docker_uri and name_template):
        raise click.ClickException("Missing DOCKER_URI or DO_NAMES in configuration")

    try:
        client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
    except Exception as e:
        raise click.ClickException(f"Failed to connect to docker manager at {docker_uri}: {e}")

    try:
        result = _select_contract_candidate(client, cfg)
    except Exception as e:
        raise click.ClickException(f"Failed to select contract candidate: {e}")

    if result is None:
        click.echo("No empty node to contract.")
        return

    serial, fqdn = result
    if dry_run:
        click.echo(f"Would contract by stopping node {fqdn} (serial={serial})")
        return

    log.info(f"[contract] Selected node {fqdn} (serial={serial}) for contraction")
    ctx.invoke(stop_node, force=False, dry_run=False, node_spec=fqdn)
```

### Files to Modify

- `/Users/eric/proj/league/code-server-mono/code-server-spawner/cspawn/cli/node.py`
  - Before line 47 (before `hosts` command): insert `_running_hosts_by_node`.
  - Lines 75-83 in `hosts` body: replace inline loop with `_running_hosts_by_node(client)`.
  - Before line 1823 (before `contract_node`): insert `_select_contract_candidate`.
  - Lines 1823-1894 (`contract_node`): replace with new implementation above.

### Files to Create

None (besides tests).

### Testing Plan

**New tests** (add to `tests/test_node_contract.py` or `tests/test_node.py`):

```python
# _running_hosts_by_node
def test_running_hosts_by_node_counts_running_tasks(): ...
    # mock client.nodes.list(), client.services.list()
    # two services, each with running tasks on different nodes
    # assert correct counts

def test_running_hosts_by_node_skips_non_running_tasks(): ...
    # task state = "shutdown"; assert not counted

# _select_contract_candidate
def test_select_candidate_returns_none_when_all_loaded(): ...
    # all nodes have running_hosts > 0
    # assert returns None

def test_select_candidate_returns_none_when_no_eligible_nodes(): ...
    # no workers match DO_NAMES pattern
    # assert returns None

def test_select_candidate_picks_empty_node(): ...
    # one loaded node, one empty node
    # assert returns the empty node

def test_select_candidate_smallest_capacity_first(): ...
    # two empty nodes: large (cap=14) and small (cap=6)
    # assert small is selected

def test_select_candidate_newest_serial_tiebreaker(): ...
    # two empty small nodes: serial 3 and serial 5
    # assert serial 5 (newest) selected

def test_select_candidate_skips_manager(): ...
    # empty node with role=manager; assert returns None

def test_select_candidate_skips_leader(): ...
    # node with ManagerStatus.Leader=True; assert not selected

def test_select_candidate_unlabeled_node_uses_default_capacity(): ...
    # node has no cs.capacity label; DEFAULT_CAPACITY=6
    # assert capacity used in sort is 6

# contract_node integration
def test_contract_dry_run_prints_candidate(): ...
    # mock _select_contract_candidate to return (5, "swarm5.dojtl.net")
    # assert "Would contract" in output, stop_node NOT called

def test_contract_exits_cleanly_when_no_empty_node(): ...
    # mock _select_contract_candidate returns None
    # assert "No empty node to contract." output, exit code 0
```

**Existing tests**: Run `uv run pytest` to confirm no regressions in `hosts` command
behavior after the `_running_hosts_by_node` refactor.

### Documentation Updates

- Update `contract_node` docstring and help text to state "only removes empty nodes" and
  direct operators to `cspawnctl node stop <node>` for forced removal.
- Document the behavioral change in the sprint's `CHANGES` or release notes (out of scope
  for this ticket; the programmer may add a brief note to the commit message).
