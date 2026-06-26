---
status: pending
---

# Multi-Size Node Provisioning + `NODE_TIERS` config model

**Audience:** an engineer or agent working on the code-server-spawner's swarm
node provisioning. This issue makes node provisioning **size-aware** — today
`expand` can only create one droplet size (`DO_SIZE`) — and records each node's
host capacity as a swarm label so a future autoscaler can reason about it.

> **Provenance:** distilled from the Plan-agent transcript
> `.clasi/log/019-Plan.md` (2026-06-22). That log is an ephemeral planning
> transcript; this issue is the durable artifact. Foundation for
> [[node-autoscaling-control-loop]] and [[instructor-cluster-presize]], which
> need per-node capacity and tier-aware provisioning.

---

## TL;DR

Introduce a `NODE_TIERS` config model (named size tiers, each with a DO slug and
a host capacity), make `expand` accept a `--tier`, stamp `cs.tier` / `cs.capacity`
labels on each node at join, backfill labels on the existing swarm1–swarm5, and
make `contract` capacity-aware (only remove empty nodes, smallest tier first).
Also pin docker-ce in cloud-init so automated joins stop failing on version
mismatch.

---

## Problem / current state

- `expand` (`cspawn/cli/node.py:1687-1821`) hardcodes a single `do_size =
  cfg.get("DO_SIZE", ...)` — it cannot create large vs. small nodes.
- There is no per-node capacity signal. Nothing records how many hosts a node
  can hold, so scale-down and any autoscaler can only guess.
- `_ensure_label_on_node` (`node.py:543-568`) hardcodes `Labels[key] = "true"`
  and **cannot express `cs.tier=large`** — a key=value-capable helper is needed.
- `contract_node` (`node.py:1823-1894`) removes the **highest serial** node with
  **no emptiness check** — it can drain a node with live student sessions.
- cloud-init (`config/cloud-init/swarm-node-init-v2.yaml:4-7`) installs
  `docker.io`, which conflicts with the image's docker-ce and, combined with the
  manager/node major-version equality check (`node.py:974`), aborts joins. See
  [[swarm-node-gotchas]].

---

## Design

### 1. Config: single JSON key `NODE_TIERS`

Add to `config/*/public.env` (example values):

```
# Ordered list. First entry is the DEFAULT tier unless DEFAULT_TIER is set.
NODE_TIERS=[{"name":"small","slug":"s-4vcpu-8gb-amd","capacity":6},{"name":"large","slug":"s-8vcpu-16gb-amd","capacity":14}]
DEFAULT_TIER=small
DO_SIZE=s-4vcpu-8gb-amd          # retained for backward compat / fallback
DEFAULT_CAPACITY=6               # capacity assumed for unlabeled/unknown nodes
```

Chosen over parallel scalar keys (`DO_SIZE_SMALL`, `MAX_HOSTS_SMALL`) because the
JSON list scales to N tiers, matches the existing `ADMIN_EMAILS` /
`PLACEMENT_CONSTRAINTS` JSON-in-flat-env precedent, and keeps name↔slug↔capacity
grouped so they can't drift. `Config` is raw dotenv strings — JSON keys must be
parsed by a helper, never read raw.

**Backward compat:** if `NODE_TIERS` is absent, synthesize one tier
`[{"name":"default","slug":<DO_SIZE>,"capacity":<DEFAULT_CAPACITY or 6>}]`.

### 2. New helper module `cspawn/cs_docker/tiers.py`

```python
@dataclass(frozen=True)
class Tier:
    name: str; slug: str; capacity: int

def load_tiers(cfg) -> list[Tier]        # parse + validate NODE_TIERS, else synthetic default
def default_tier(cfg) -> Tier            # DEFAULT_TIER, else tiers()[0]
def tier_by_name(cfg, name) -> Tier|None
def tier_for_slug(cfg, slug) -> Tier|None   # reverse lookup for backfill
def default_capacity(cfg) -> int         # DEFAULT_CAPACITY, else 6
```

All config readers go through these — no raw `cfg.get("NODE_TIERS")`.

### 3. Size-aware `expand`

Add `--tier` (not `--size`; tier keeps capacity coupled to slug):

```python
@click.option("--tier", "tier_name", required=False, type=str,
              help="Node size tier from NODE_TIERS (default: DEFAULT_TIER).")
```

Resolve to `do_size = tier.slug` at the top of `expand`. Thread `tier` through
`expand` → `_create_droplet` → `_join_swarm` so the join/label step knows
tier/capacity. **Single serial sequence kept** — `_get_next_serial` already
derives the next number from all `swarm*` hostnames regardless of size; serial is
a DNS/identity concern, orthogonal to size. Tier lives in labels, not the name.

### 4. Per-node capacity via labels

New key=value-capable helper near `node.py:543`:

```python
def _ensure_node_labels(manager_client, node_name, labels: dict[str,str], log=None) -> bool:
    """Merge key=value labels into the node's Spec.Labels. Idempotent."""
```

Label scheme (additive to the existing `code-host-user=true` flag):

```
cs.tier=<name>        e.g. cs.tier=large
cs.capacity=<int>     e.g. cs.capacity=14
```

Apply in `_join_swarm` (`node.py:1074-1104`) after the existing
`SWARM_NODE_LABEL` block, reusing the IP→node match loop; skip `cs.*` when
`tier is None` (e.g. `expand --join` on a pre-existing node). Read back:

```python
def node_capacity(node_attrs, cfg) -> int:
    labels = node_attrs.get("Spec", {}).get("Labels", {})
    try: return int(labels["cs.capacity"])
    except (KeyError, ValueError): return default_capacity(cfg)
```

### 5. Backfill command `node label-backfill`

Read-only by default, `--apply` to write. For each swarm node matching `DO_NAMES`
that lacks `cs.tier`: resolve its DO droplet, read `size_slug`, map via
`tier_for_slug`, and (with `--apply`) stamp `cs.tier`/`cs.capacity`. Print a
table: node | slug | inferred tier | capacity | action. Managers (swarm1) get
labeled for completeness but are excluded from placement by
`PLACEMENT_CONSTRAINTS`.

### 6. Capacity-aware `contract`

Rework `contract_node` selection: **only remove empty nodes** (`host_count == 0`,
from the live `node hosts` query); among empties sort `(capacity ASC, serial
DESC)` → smallest tier, newest. If none empty: "No empty node to contract" and
stop (never drain live users). Extract the selection as
`_select_contract_candidate(client, cfg) -> (serial, fqdn)|None` so a future
autoscaler reuses it. Factor the per-node running-host count into a shared
`_running_hosts_by_node(client) -> dict[short,int]`.

### 7. cloud-init docker pin (prerequisite, unblocks reliable joins)

Edit `config/cloud-init/swarm-node-init-v2.yaml`: drop `docker.io` from
`packages:`; pin & hold docker-ce at the manager's major/version (27.4.1 today)
via a `runcmd` before `systemctl enable --now docker`:

```yaml
runcmd:
  - apt-get update
  - >-
    apt-get install -y --allow-downgrades --allow-change-held-packages
    docker-ce=5:27.4.1-1~ubuntu.20.04~focal
    docker-ce-cli=5:27.4.1-1~ubuntu.20.04~focal
  - apt-mark hold docker-ce docker-ce-cli
  - systemctl enable --now docker
```

Makes the join preflight (`node.py:974` major-version equality) pass without the
manual downgrade runbook. Pinned string must match the image's Ubuntu codename
(focal). See [[swarm-node-gotchas]].

---

## Implementation touch points / critical files

- `cspawn/cs_docker/tiers.py` — **new** (`Tier`, `load_tiers`, `default_tier`,
  `tier_for_slug`, `default_capacity`).
- `cspawn/cli/node.py` — `--tier` on `expand` (1687-1821); thread `tier` through
  `_create_droplet` (719-852) / `_join_swarm` (898-1104); new `_ensure_node_labels`
  (near 543); capacity-aware `contract` + `_select_contract_candidate`
  (1823-1894); `_running_hosts_by_node` extracted from `hosts` (48-96); new
  `node label-backfill`.
- `config/prod/public.env` + `config/local-prod/public.env` + `config/devel/public.env`
  — `NODE_TIERS`, `DEFAULT_TIER`, `DEFAULT_CAPACITY` (keep `DO_SIZE` fallback).
- `config/cloud-init/swarm-node-init-v2.yaml` — drop `docker.io`, pin+hold docker-ce.

## Sequencing

1. cloud-init pin (§7) — independent, do first.
2. `tiers.py` + config keys (§1-2) — foundation.
3. `_ensure_node_labels` + size-aware `expand`/`_join_swarm` (§3-4).
4. `label-backfill` (§5).
5. capacity-aware `contract` (§6) — run backfill in prod first.

## Open / to confirm

- Exact large slug (`s-8vcpu-16gb-amd`?) and the two capacity numbers (6 / 14?).
- Keep `DO_SIZE` as fallback (recommended: yes).
- Behavior when a live droplet's slug isn't in `NODE_TIERS` (skip vs. assume default).
- Should `contract` ever drain a lightly-loaded (non-empty) node? Recommended: no.
- Static docker-ce version pin now vs. config-driven (`DOCKER_CE_VERSION`) later.
