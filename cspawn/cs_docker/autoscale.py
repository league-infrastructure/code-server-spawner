"""
cspawn/cs_docker/autoscale.py — Pure decision functions for the autoscale control loop.

Architecture split
------------------
This module is the **pure decision layer**: all functions are side-effect-free.
They take plain Python data (dicts, lists, dataclasses, config) and return plain
data. No Docker, DigitalOcean, database, or network I/O happens here. That makes
every function fully unit-testable without infrastructure.

The companion orchestrator (``run_autoscale`` in ticket 004) handles all I/O:
  - ``gather_cluster_state`` reads the live Swarm node list, per-node task counts,
    and pending CodeHost rows from the DB.
  - ``apply_plan`` executes scale-up (add_node) or scale-down (graceful_remove_node).
  - ``run_autoscale`` is the single entry point: kill-switch → lock → gather →
    build_plan → apply → log → release.

Demand-signal seam (``estimate_demand``)
-----------------------------------------
``estimate_demand`` is designed as a clean, swappable seam. All I/O happens in
the caller (``gather_cluster_state``); this function only does math. The next
sprint (instructor-cluster-presize) will swap the demand source from
``Class.running`` to purge-window timestamps by changing only what the caller
fetches and how the class dicts are populated — the function signature and call
site do not change.

Config keys read here (all with safe defaults):
  AUTOSCALE_HEADROOM              int   default 2
  AUTOSCALE_ROSTER_FRACTION       float default 0.8
  AUTOSCALE_MAX_ADD_PER_CYCLE     int   default 2
  AUTOSCALE_MAX_REMOVE_PER_CYCLE  int   default 1
  AUTOSCALE_SCALEDOWN_COOLDOWN_MIN int  default 30
  AUTOSCALE_MIN_WORKER_NODES      int   default 1
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import ceil
from typing import TYPE_CHECKING

from cspawn.cs_docker.tiers import load_tiers, node_capacity

if TYPE_CHECKING:
    pass  # no runtime imports of I/O-heavy modules

__all__ = [
    "NodeView",
    "ClusterState",
    "ScalePlan",
    "capacity_for_node",
    "assess_cluster",
    "estimate_demand",
    "compute_deficit",
    "plan_scale_up",
    "plan_scale_down",
    "build_plan",
]


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _cfg_int(cfg, key: str, default: int) -> int:
    """Read an integer config key; return *default* if absent or un-parseable."""
    raw = cfg.get(key)
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return default


def _cfg_float(cfg, key: str, default: float) -> float:
    """Read a float config key; return *default* if absent or un-parseable."""
    raw = cfg.get(key)
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return default


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class NodeView:
    """Lightweight snapshot of one Swarm node, populated from raw node attrs."""
    short: str                  # short hostname, e.g. "swarm2"
    fqdn: str                   # fully-qualified, e.g. "swarm2.dojtl.net"
    size_slug: str | None       # DO droplet slug if known, else None
    capacity: int               # from cs.capacity label or tier/default fallback
    running_hosts: int          # live Swarm task count on this node
    is_manager: bool            # True if node role == "manager"
    is_leader: bool             # True if ManagerStatus.Leader is True
    serial: int | None          # numeric suffix of hostname, e.g. 2 for "swarm2"


@dataclass
class ClusterState:
    """Aggregate view of the Swarm cluster built from pre-fetched data."""
    nodes: list[NodeView]
    pending_hosts: int          # CodeHost rows starting but not yet Swarm-placed

    @property
    def total_capacity(self) -> int:
        """Sum of capacities across non-manager (worker) nodes only."""
        return sum(n.capacity for n in self.nodes if not n.is_manager)

    @property
    def total_load(self) -> int:
        """Sum of running_hosts across all nodes (managers included for accuracy)."""
        return sum(n.running_hosts for n in self.nodes)

    @property
    def excess_capacity(self) -> int:
        """Spare host slots: total worker capacity minus total running hosts."""
        return self.total_capacity - self.total_load


@dataclass
class ScalePlan:
    """Output of build_plan: what the orchestrator should do this cycle."""
    add_large: int
    add_small: int
    remove_nodes: list[str] = field(default_factory=list)  # fqdns to remove
    purge_first: bool = False   # always True when remove_nodes is non-empty
    reason: str = ""

    def summary(self) -> str:
        """Return a single-line structured log string suitable for journald/logfmt."""
        return (
            f"autoscale plan="
            f"add_large={self.add_large} "
            f"add_small={self.add_small} "
            f"remove={len(self.remove_nodes)} "
            f"purge_first={self.purge_first} "
            f'reason="{self.reason}"'
        )


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def capacity_for_node(node_attrs: dict, cfg) -> int:
    """Return the host capacity for a raw Swarm node attrs dict.

    Reads ``Spec.Labels["cs.capacity"]`` first; falls back to
    ``node_capacity(node_attrs, cfg)`` from ``cspawn.cs_docker.tiers``,
    which itself falls back to ``DEFAULT_CAPACITY`` (6).
    """
    labels = (node_attrs.get("Spec") or {}).get("Labels") or {}
    raw = labels.get("cs.capacity")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return node_capacity(node_attrs, cfg)


def _extract_serial(hostname: str) -> int | None:
    """Return the trailing integer from a hostname, e.g. 2 from 'swarm2'."""
    m = re.search(r"(\d+)$", hostname.split(".")[0])
    return int(m.group(1)) if m else None


def assess_cluster(
    node_dicts: list[dict],
    host_counts: dict[str, int],
    pending: int,
    cfg,
) -> ClusterState:
    """Build a ``ClusterState`` from raw Swarm node attrs and pre-fetched counts.

    Args:
        node_dicts:  Raw dicts from ``docker.nodes.list()`` — each is ``node.attrs``.
        host_counts: ``{short_hostname: running_task_count}`` from
                     ``count_hosts_per_node``.
        pending:     Count of CodeHost rows that are starting but not yet placed.
        cfg:         App config object.

    Both worker and manager nodes are included in ``nodes``; the
    ``is_manager`` / ``is_leader`` flags distinguish them. Only worker nodes
    contribute to ``total_capacity``.
    """
    views: list[NodeView] = []
    for attrs in node_dicts:
        spec = attrs.get("Spec") or {}
        desc = attrs.get("Description") or {}
        manager_status = attrs.get("ManagerStatus") or {}

        hostname = desc.get("Hostname") or ""
        short = hostname.split(".")[0] if hostname else (attrs.get("ID") or "?")
        fqdn = hostname if "." in hostname else short

        role = (spec.get("Role") or "").lower()
        is_manager = role == "manager"
        is_leader = bool(manager_status.get("Leader"))

        # Size slug from Resources or Labels
        resources = desc.get("Resources") or {}
        size_slug: str | None = (spec.get("Labels") or {}).get("cs.size_slug") or None

        cap = capacity_for_node(attrs, cfg)
        running = host_counts.get(short, 0)
        serial = _extract_serial(short)

        views.append(NodeView(
            short=short,
            fqdn=fqdn,
            size_slug=size_slug,
            capacity=cap,
            running_hosts=running,
            is_manager=is_manager,
            is_leader=is_leader,
            serial=serial,
        ))

    return ClusterState(nodes=views, pending_hosts=pending)


def estimate_demand(classes: list[dict], hosts: list[dict], cfg) -> int:
    """Estimate the total number of host slots needed right now.

    This is the demand-signal seam. The caller (``gather_cluster_state``)
    is responsible for all I/O; this function only does arithmetic.

    Formula:
        live_load   = count of host dicts where not is_mia and not is_purgeable
        pending     = count of host dicts where app_state not in ('ready',)
                      and not is_mia
        prescale    = sum(ceil(len(c['students']) * ROSTER_FRACTION)
                          for c in classes if c['running'] and c['stops_at'] > now)
        demand      = max(live_load + pending, prescale) + HEADROOM

    Config keys:
        AUTOSCALE_HEADROOM          (int,   default 2)
        AUTOSCALE_ROSTER_FRACTION   (float, default 0.8)

    The next sprint (instructor-cluster-presize) will change what the caller
    puts in ``classes`` (purge-window timestamps instead of ``Class.running``),
    but the signature and math here stay identical.
    """
    headroom = _cfg_int(cfg, "AUTOSCALE_HEADROOM", 2)
    roster_fraction = _cfg_float(cfg, "AUTOSCALE_ROSTER_FRACTION", 0.8)

    now = datetime.now(timezone.utc)

    live_load = sum(
        1 for h in hosts
        if not h.get("is_mia") and not h.get("is_purgeable")
    )
    pending = sum(
        1 for h in hosts
        if h.get("app_state") not in ("ready",) and not h.get("is_mia")
    )

    prescale = 0
    for c in classes:
        if not c.get("running"):
            continue
        stops_at = c.get("stops_at")
        if stops_at is None:
            continue
        # Accept both datetime objects and ISO strings (caller may pass either)
        if isinstance(stops_at, str):
            try:
                stops_at = datetime.fromisoformat(stops_at)
            except ValueError:
                continue
        # Make timezone-aware if naive
        if stops_at.tzinfo is None:
            stops_at = stops_at.replace(tzinfo=timezone.utc)
        if stops_at > now:
            student_count = len(c.get("students") or [])
            prescale += ceil(student_count * roster_fraction)

    return max(live_load + pending, prescale) + headroom


def compute_deficit(state: ClusterState, demand: int, cfg) -> int:
    """Return the number of additional host slots needed (0 if none).

    ``deficit = max(0, demand - state.total_capacity)``
    """
    return max(0, demand - state.total_capacity)


def plan_scale_up(deficit: int, cfg) -> tuple[int, int]:
    """Greedy bin-pack to cover *deficit* host slots.

    Uses ``load_tiers(cfg)`` to resolve large and small tier capacities.
    If only one tier exists, both roles use the same tier.

    Algorithm:
        add_large = deficit // cap_large
        rem       = deficit %  cap_large
        if rem == 0:           add_small = 0
        elif rem <= cap_small: add_small = 1
        else:                  add_large += 1  (one more large is cheaper than two nodes)

    Result is clamped so ``add_large + add_small <= AUTOSCALE_MAX_ADD_PER_CYCLE``.
    ``add_small`` is reduced first; ``add_large`` is reduced last.

    Returns:
        ``(add_large, add_small)``
    """
    if deficit <= 0:
        return (0, 0)

    tiers = load_tiers(cfg)
    # Sort ascending by capacity so index 0 = smallest, -1 = largest
    sorted_tiers = sorted(tiers, key=lambda t: t.capacity)
    cap_large = sorted_tiers[-1].capacity
    cap_small = sorted_tiers[0].capacity

    add_large = deficit // cap_large
    rem = deficit % cap_large

    if rem == 0:
        add_small = 0
    elif rem <= cap_small:
        add_small = 1
    else:
        # Remainder can't be covered by one small node; add another large
        add_large += 1
        add_small = 0

    max_add = _cfg_int(cfg, "AUTOSCALE_MAX_ADD_PER_CYCLE", 2)
    total = add_large + add_small
    if total > max_add:
        # Reduce add_small first, then add_large
        excess = total - max_add
        reduce_small = min(add_small, excess)
        add_small -= reduce_small
        excess -= reduce_small
        add_large = max(0, add_large - excess)

    return (add_large, add_small)


def plan_scale_down(
    state: ClusterState,
    demand: int,
    cfg,
    now: datetime,
    empty_since: dict[str, datetime],
) -> list[NodeView]:
    """Select zero-load, cooled-down, non-manager worker nodes to remove.

    Selection criteria (all must be true):
      - Not a manager, not a leader.
      - ``running_hosts == 0``.
      - Has been empty for at least ``AUTOSCALE_SCALEDOWN_COOLDOWN_MIN`` minutes
        (looked up by fqdn in *empty_since*; nodes not in the dict are skipped).
      - Removing it would still leave ``excess_capacity > candidate.capacity +
        AUTOSCALE_HEADROOM`` (dead-band guard).
      - Removing it would still leave ``>= AUTOSCALE_MIN_WORKER_NODES`` workers.

    Candidates are sorted by serial (descending, i.e. highest serial removed first).
    At most ``AUTOSCALE_MAX_REMOVE_PER_CYCLE`` nodes are returned.

    Args:
        state:       Current cluster snapshot.
        demand:      Estimated demand (used implicitly via excess_capacity dead-band).
        cfg:         App config.
        now:         Current UTC datetime (injected for testability).
        empty_since: ``{fqdn: datetime_became_empty}`` — tracked by the orchestrator.
    """
    cooldown_min = _cfg_int(cfg, "AUTOSCALE_SCALEDOWN_COOLDOWN_MIN", 30)
    min_workers = _cfg_int(cfg, "AUTOSCALE_MIN_WORKER_NODES", 1)
    max_remove = _cfg_int(cfg, "AUTOSCALE_MAX_REMOVE_PER_CYCLE", 1)
    headroom = _cfg_int(cfg, "AUTOSCALE_HEADROOM", 2)

    # Count total workers (non-managers) before removal
    total_workers = sum(1 for n in state.nodes if not n.is_manager)

    # Build candidates sorted by serial descending (highest serial first)
    candidates = sorted(
        (n for n in state.nodes if not n.is_manager and not n.is_leader and n.running_hosts == 0),
        key=lambda n: (n.serial if n.serial is not None else -1),
        reverse=True,
    )

    selected: list[NodeView] = []
    remaining_excess = state.excess_capacity
    workers_left = total_workers

    for node in candidates:
        if len(selected) >= max_remove:
            break

        # Check cooldown
        became_empty = empty_since.get(node.fqdn)
        if became_empty is None:
            continue
        # Make timezone-aware if naive
        if became_empty.tzinfo is None:
            became_empty = became_empty.replace(tzinfo=timezone.utc)
        elapsed_min = (now - became_empty).total_seconds() / 60
        if elapsed_min < cooldown_min:
            continue

        # Dead-band guard: removing this node must still leave headroom
        if remaining_excess <= node.capacity + headroom:
            continue

        # Min-worker guard
        if workers_left - 1 < min_workers:
            continue

        selected.append(node)
        remaining_excess -= node.capacity
        workers_left -= 1

    return selected


def build_plan(
    state: ClusterState,
    demand: int,
    cfg,
    now: datetime,
    empty_since: dict[str, datetime],
) -> ScalePlan:
    """Decide what the orchestrator should do this cycle.

    Priority (never both up and down in one cycle):
      1. If ``compute_deficit > 0`` → scale up (no removes).
      2. Elif ``plan_scale_down`` returns candidates → scale down (no adds).
      3. Else → hold (within dead-band).

    Args:
        state:       Current cluster snapshot.
        demand:      Estimated demand from ``estimate_demand``.
        cfg:         App config.
        now:         Current UTC datetime (injected for testability).
        empty_since: ``{fqdn: datetime_became_empty}`` for scale-down cooldown.

    Returns:
        A ``ScalePlan`` with either adds, removes, or neither.
    """
    deficit = compute_deficit(state, demand, cfg)

    if deficit > 0:
        add_large, add_small = plan_scale_up(deficit, cfg)
        return ScalePlan(
            add_large=add_large,
            add_small=add_small,
            remove_nodes=[],
            purge_first=False,
            reason=f"scale-up: deficit={deficit} add_large={add_large} add_small={add_small}",
        )

    removals = plan_scale_down(state, demand, cfg, now, empty_since)
    if removals:
        return ScalePlan(
            add_large=0,
            add_small=0,
            remove_nodes=[n.fqdn for n in removals],
            purge_first=True,
            reason=f"scale-down: removing {[n.fqdn for n in removals]}",
        )

    return ScalePlan(
        add_large=0,
        add_small=0,
        remove_nodes=[],
        purge_first=False,
        reason="hold: within dead-band",
    )
