---
id: "003"
title: "_ensure_node_labels + size-aware expand and join"
status: open
use-cases: [SUC-001, SUC-002]
depends-on: ["002"]
github-issue: ""
issue: ""
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# _ensure_node_labels + size-aware expand and join

## Description

Wire tier awareness into the node provisioning flow in `cspawn/cli/node.py`. This ticket
has three closely coupled changes that must be delivered together:

1. **`_ensure_node_labels`** — a new key=value label helper. The existing
   `_ensure_label_on_node` hardcodes `value="true"` (node.py:557-559) and cannot express
   `cs.tier=large`. The new function accepts a `dict[str, str]` and merges all entries.
   The existing function is not changed.

2. **`expand --tier`** — a new CLI option that resolves to a `Tier` object from `tiers.py`
   and drives `do_size`. Replaces the hardcoded `cfg.get("DO_SIZE", "s-1vcpu-1gb")` at
   node.py:1708.

3. **Thread `tier` through `_create_droplet` and `_join_swarm`** — so the join step knows
   which tier was used and can stamp `cs.tier` / `cs.capacity` labels on the node via
   `_ensure_node_labels`.

**Depends on 002**: requires `cspawn/cs_docker/tiers.py` and `load_tiers` / `default_tier`
/ `tier_by_name`.

## Acceptance Criteria

- [ ] `_ensure_node_labels(manager_client, node_name, labels: dict[str, str], log=None) -> bool` exists in `node.py`, idempotently merges all key=value pairs into `Spec.Labels`, and returns `True` if any label was changed.
- [ ] `_ensure_node_labels` skips keys whose existing value already matches (no spurious swarm API calls).
- [ ] `_ensure_label_on_node` is unchanged (still used by `SWARM_NODE_LABEL` path).
- [ ] `expand` accepts `--tier <name>` (option name `tier_name`, not required).
- [ ] `expand --tier large` resolves to the `large` tier from `NODE_TIERS` and creates a droplet with the large slug.
- [ ] `expand` without `--tier` uses `default_tier(cfg)` (respects `DEFAULT_TIER` config or first tier).
- [ ] `expand --tier <unknown>` raises a `ClickException` with a descriptive message listing valid tier names.
- [ ] `do_size` in `expand` is derived from `tier.slug` (line ~1708 replaced).
- [ ] `_create_droplet` accepts a `tier: Tier | None = None` keyword argument; uses `tier.slug` as `do_size` when provided.
- [ ] `_join_swarm` accepts a `tier: Tier | None = None` keyword argument.
- [ ] After the existing `SWARM_NODE_LABEL` block in `_join_swarm` (line ~1073), `_ensure_node_labels` is called with `{"cs.tier": tier.name, "cs.capacity": str(tier.capacity)}` when `tier is not None`.
- [ ] `_join_swarm` skips `cs.*` labeling when `tier is None` (standalone `expand --join` path).
- [ ] The IP-to-node match loop used for `SWARM_NODE_LABEL` (line ~1081-1093) is reused for `cs.*` labels — no separate node list walk.
- [ ] `cspawnctl node expand --tier small` end-to-end (with mocked DO + swarm) stamps correct labels.
- [ ] Unit tests for `_ensure_node_labels` pass (see Testing Plan).

## Implementation Plan

### Approach

All changes are in `cspawn/cli/node.py`. The structure is:

1. Add `_ensure_node_labels` near `_ensure_label_on_node` (line ~569, after it).
2. Modify `expand` to add `--tier` option and resolve tier before the `_create_droplet` call.
3. Add `tier` parameter to `_create_droplet`.
4. Add `tier` parameter to `_join_swarm` and add `cs.*` label calls after the existing SWARM_NODE_LABEL block.

### New function: `_ensure_node_labels` (insert after line 568)

```python
def _ensure_node_labels(
    manager_client: docker.DockerClient,
    node_name: str,
    labels: dict[str, str],
    log=None,
) -> bool:
    """Merge key=value labels into the swarm node's Spec.Labels. Idempotent.

    Returns True if any label was changed, False if all were already present
    with matching values. Skips gracefully on errors (logs warning).

    Uses the same low-level API as _ensure_label_on_node for SDK 2.0 compat.
    """
    if not labels:
        return False
    try:
        short = node_name.split(".")[0] if node_name else node_name
        node_obj = _find_swarm_node(manager_client, node_name, short)
        if not node_obj:
            return False
        info = manager_client.api.inspect_node(node_obj.id)
        version = ((info or {}).get("Version", {}) or {}).get("Index")
        spec = ((info or {}).get("Spec", {}) or {}).copy()
        existing = (spec.get("Labels") or {}).copy()
        # Check which labels actually need updating
        to_set = {k: v for k, v in labels.items() if existing.get(k) != v}
        if not to_set:
            return False  # all already set correctly
        existing.update(to_set)
        spec["Labels"] = existing
        manager_client.api.update_node(node_obj.id, version, spec)
        if log:
            for k, v in to_set.items():
                log.info(f"[expand] Applied node label '{k}={v}' on {node_name}")
        return True
    except Exception as e:
        if log:
            log.warning(f"[expand] Failed to apply node labels on {node_name}: {e}")
        return False
```

### Modify `expand` command (line ~1687)

Add new decorator before `@click.pass_context`:
```python
@click.option("--tier", "tier_name", required=False, type=str,
              help="Node size tier from NODE_TIERS (default: DEFAULT_TIER). "
                   "See 'cspawnctl node tiers' or NODE_TIERS config key.")
```

Add `tier_name: str | None` to the `expand` function signature.

Replace line 1708 (`do_size = cfg.get("DO_SIZE", "s-1vcpu-1gb")`):
```python
from cspawn.cs_docker.tiers import load_tiers, default_tier, tier_by_name
if tier_name:
    tier = tier_by_name(cfg, tier_name)
    if tier is None:
        valid = [t.name for t in load_tiers(cfg)]
        raise click.ClickException(
            f"Unknown tier '{tier_name}'. Valid tiers: {', '.join(valid)}"
        )
else:
    tier = default_tier(cfg)
do_size = tier.slug
```

Update the `_create_droplet` call at line ~1755 to pass `tier=tier`.

Update the `_join_swarm` call at line ~1786 to pass `tier=tier`.

### Modify `_create_droplet` signature (line 719)

Add `tier: "Tier | None" = None` parameter. Inside the function, replace:
```python
size_slug=do_size,
```
with:
```python
size_slug=tier.slug if tier is not None else do_size,
```
(Keep `do_size` parameter for backward compat with any direct callers; `tier` takes precedence.)

### Modify `_join_swarm` (line 898)

Add `tier: "Tier | None" = None` parameter to signature.

After the existing label block (line ~1104, after `except Exception: pass`), add:
```python
    # Apply cs.tier and cs.capacity labels if tier is known
    if tier is not None:
        try:
            deadline_labels = time.time() + 90
            cs_applied = False
            while time.time() < deadline_labels and not cs_applied:
                try:
                    for n in manager_client.nodes.list():
                        try:
                            info = manager_client.api.inspect_node(n.id)
                            addr = ((info or {}).get("Status", {}) or {}).get("Addr")
                            if addr == ip:
                                name = ((info or {}).get("Description", {}) or {}).get("Hostname") or ""
                                if name:
                                    cs_applied = _ensure_node_labels(
                                        manager_client, name,
                                        {"cs.tier": tier.name, "cs.capacity": str(tier.capacity)},
                                        log=log,
                                    ) or cs_applied
                                    break
                        except Exception:
                            continue
                except Exception:
                    pass
                if not cs_applied:
                    time.sleep(3)
        except Exception:
            pass
```

Note: the IP-variable `ip` is in scope at this point in `_join_swarm` (set at line ~916).
The `tier` type annotation uses a string literal `"Tier | None"` to avoid import-time
circular dependency if needed — or import `Tier` at the top of node.py from `tiers`.

### Files to Modify

- `/Users/eric/proj/league/code-server-mono/code-server-spawner/cspawn/cli/node.py`
  - After line 568: insert `_ensure_node_labels` function.
  - Line ~1687: add `--tier` decorator to `expand`.
  - Line ~1696: add `tier_name: str | None` to `expand` signature.
  - Line ~1708: replace `do_size = cfg.get(...)` with tier resolution block.
  - Line ~1755: add `tier=tier` to `_create_droplet` call.
  - Line ~1786: add `tier=tier` to `_join_swarm` call.
  - Line 719: add `tier: "Tier | None" = None` to `_create_droplet` signature; update `size_slug`.
  - Line 898: add `tier: "Tier | None" = None` to `_join_swarm` signature; add `cs.*` label block after existing label block (~line 1104).

### Files to Create

None (besides the tests file).

### Testing Plan

**New tests** (add to `tests/test_node.py` or a new `tests/test_node_labels.py`):

```python
# _ensure_node_labels
def test_ensure_node_labels_applies_all_keys(): ...
    # mock inspect_node to return node with empty Labels
    # assert update_node called with merged labels

def test_ensure_node_labels_skips_if_already_set(): ...
    # mock Labels already containing both keys with correct values
    # assert update_node NOT called, returns False

def test_ensure_node_labels_partial_update(): ...
    # one label already correct, one missing
    # assert only missing label is in the update call

def test_ensure_node_labels_returns_false_on_error(): ...
    # mock inspect_node to raise; assert returns False, no exception propagated
```

**Existing tests**: Run `uv run pytest` to confirm no regressions in the `expand` flow.

### Documentation Updates

Update `expand` command docstring to mention `--tier`. The `_ensure_node_labels` function
docstring is the spec for the label merge contract.
