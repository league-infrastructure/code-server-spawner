---
id: '004'
title: node label-backfill command
status: done
use-cases:
- SUC-004
depends-on:
- '002'
- '003'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# node label-backfill command

## Description

Add a new `node label-backfill` CLI subcommand to `cspawn/cli/node.py`. This command
stamps `cs.tier` and `cs.capacity` swarm labels on existing nodes (swarm1–swarm5) that
predate the label scheme introduced in ticket 003. Without this, `contract`'s capacity-
aware selection (ticket 005) cannot distinguish small from large nodes until a node is
re-provisioned through the updated `expand` flow.

The command works as follows:
1. Connect to the swarm manager and list all nodes matching `DO_NAMES`.
2. For each node that lacks `cs.tier`: resolve its DO droplet via the DO API,
   read the droplet's `size_slug`, map it to a tier using `tier_for_slug`.
3. Print a table (always): node | slug | inferred tier | capacity | action.
4. With `--apply`: call `_ensure_node_labels` to write the labels.

Default mode is dry-run (read-only). `--apply` is required to write labels.

**Depends on 002** (tiers.py, `tier_for_slug`) and **003** (`_ensure_node_labels`).

## Acceptance Criteria

- [x] `cspawnctl node label-backfill` runs without `--apply` and prints a table without modifying any labels.
- [x] Table columns: `NODE`, `SIZE_SLUG`, `INFERRED_TIER`, `CAPACITY`, `ACTION`.
- [x] `ACTION` column shows `would-apply` in dry-run; `applied` or `already-set` in `--apply` mode.
- [x] Nodes that already have `cs.tier` set are shown as `already-set` and skipped.
- [x] `cspawnctl node label-backfill --apply` stamps `cs.tier` and `cs.capacity` on nodes that lack `cs.tier`.
- [x] Backfill is idempotent: running `--apply` twice produces no errors and no duplicate API calls.
- [x] When a node's DO droplet has a slug not in `NODE_TIERS`, the node is skipped with a `WARN: unknown slug` in the `ACTION` column and a warning printed to stderr.
- [x] Manager nodes (swarm1 if it is the manager) are included in the table and labeled (for completeness) but the existing `PLACEMENT_CONSTRAINTS` prevents them from receiving code-server tasks.
- [x] Missing `DO_TOKEN` raises a `ClickException` with a clear message.
- [x] The command is registered under the `node` group and appears in `cspawnctl node --help`.

## Implementation Plan

### Approach

Add a new `@node.command(name="label-backfill")` function to `cspawn/cli/node.py`.
Reuse the existing droplet-listing pattern from `_list_droplets_by_tag_or_project` /
`_resolve_droplet_by_spec` (around line 1126+) to find each node's DO droplet and read
its `size_slug`.

### New command structure

```python
@node.command(name="label-backfill")
@click.option("--apply", "do_apply", is_flag=True,
              help="Write cs.tier and cs.capacity labels (default: dry-run, print only).")
@click.pass_context
def label_backfill(ctx, do_apply: bool):
    """Stamp cs.tier/cs.capacity labels on existing unlabeled swarm nodes.

    Reads each node's DigitalOcean droplet size_slug, maps it to a tier via
    NODE_TIERS (tier_for_slug), and (with --apply) writes the labels.

    Safe to run multiple times: nodes with cs.tier already set are skipped.
    """
    log = get_logger(ctx)
    cfg = get_config()
    docker_uri = cfg.get("DOCKER_URI")
    do_token = cfg.get("DO_TOKEN")
    name_template = cfg.get("DO_NAMES")
    do_tag = cfg.get("DO_TAG")

    if not (docker_uri and do_token and name_template):
        raise click.ClickException("Missing DOCKER_URI, DO_TOKEN, or DO_NAMES in configuration")

    from cspawn.cs_docker.tiers import load_tiers, tier_for_slug

    tiers = load_tiers(cfg)
    if not tiers:
        raise click.ClickException("No tiers configured; check NODE_TIERS or DO_SIZE.")

    # Connect to swarm manager
    try:
        client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
    except Exception as e:
        raise click.ClickException(f"Failed to connect to docker manager: {e}")

    # Connect to DO
    mgr = digitalocean.Manager(token=do_token)

    # Fetch all droplets once (use tag filter if available)
    try:
        droplets = mgr.get_all_droplets(tag_name=do_tag) if do_tag else mgr.get_all_droplets()
    except Exception as e:
        raise click.ClickException(f"Failed to list DO droplets: {e}")
    droplet_by_name = {d.name.split(".")[0]: d for d in droplets}

    pat = _regex_from_template(name_template)

    rows = []  # list of (node_name, slug, tier_name, capacity, action)

    for n in client.nodes.list():
        attrs = n.attrs or {}
        hostname = ((attrs.get("Description") or {}).get("Hostname") or "").strip()
        if not hostname or not pat.match(hostname):
            continue
        short = hostname.split(".")[0]

        # Check existing labels
        spec_labels = ((attrs.get("Spec") or {}).get("Labels") or {})
        if "cs.tier" in spec_labels:
            rows.append((short, "—", spec_labels.get("cs.tier"), spec_labels.get("cs.capacity", "?"), "already-set"))
            continue

        # Resolve droplet
        droplet = droplet_by_name.get(short)
        if not droplet:
            rows.append((short, "?", "?", "?", "WARN: droplet not found"))
            continue

        slug = getattr(droplet, "size_slug", None) or ""
        tier = tier_for_slug(cfg, slug)
        if tier is None:
            rows.append((short, slug, "?", "?", f"WARN: unknown slug"))
            log.warning(f"[label-backfill] Node {short} has slug '{slug}' not in NODE_TIERS; skipping")
            continue

        if do_apply:
            _ensure_node_labels(client, hostname, {"cs.tier": tier.name, "cs.capacity": str(tier.capacity)}, log=log)
            action = "applied"
        else:
            action = "would-apply"

        rows.append((short, slug, tier.name, str(tier.capacity), action))

    # Print table
    click.echo(f"{'NODE':<12} {'SIZE_SLUG':<24} {'INFERRED_TIER':<16} {'CAPACITY':<10} ACTION")
    click.echo("-" * 80)
    for node, slug, tier_name, cap, action in rows:
        click.echo(f"{node:<12} {slug:<24} {tier_name:<16} {cap:<10} {action}")

    if not do_apply:
        click.echo("\n(Dry run. Re-run with --apply to write labels.)")
```

### Files to Modify

- `/Users/eric/proj/league/code-server-mono/code-server-spawner/cspawn/cli/node.py`
  - Add `label_backfill` command function. Best placement: near the other node info
    commands, after the `hosts` command (around line 97) or before `contract_node`
    (around line 1823). Prefer grouping with informational/maintenance commands.

### Files to Create

None (besides tests).

### Testing Plan

**New tests** (add to `tests/test_node_backfill.py` or `tests/test_node.py`):

```python
def test_label_backfill_dry_run_prints_table_without_writing(): ...
    # mock: client.nodes.list(), mgr.get_all_droplets(), no update_node call
    # assert update_node NOT called; output contains "would-apply"

def test_label_backfill_apply_stamps_labels(): ...
    # mock: node has no cs.tier; droplet size_slug in NODE_TIERS
    # assert _ensure_node_labels called with correct dict

def test_label_backfill_skips_already_labeled_nodes(): ...
    # node has cs.tier already set
    # assert update_node NOT called; action = "already-set"

def test_label_backfill_unknown_slug_shows_warning(): ...
    # droplet slug not in NODE_TIERS
    # assert action contains "WARN"

def test_label_backfill_idempotent_on_second_apply(): ...
    # first apply sets labels; second apply: _ensure_node_labels returns False (no-op)
```

**Existing tests**: Run `uv run pytest` to confirm no regressions.

### Documentation Updates

The command's docstring and help text are the documentation. No separate docs needed.
Operators should be instructed (in the sprint's deploy notes) to run
`cspawnctl node label-backfill` (dry run) then `--apply` on existing nodes before
ticket 005 is deployed.
