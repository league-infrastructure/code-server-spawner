"""
cspawn/cs_docker/autoscale.py — Pure decision functions + orchestrator for the autoscale loop.

Architecture split
------------------
This module is the **pure decision layer**: all functions are side-effect-free.
They take plain Python data (dicts, lists, dataclasses, config) and return plain
data. No Docker, DigitalOcean, database, or network I/O happens here. That makes
every function fully unit-testable without infrastructure.

The companion orchestrator (at the bottom of this file) handles all I/O:
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

``empty_since`` persistence
----------------------------
``run_autoscale`` persists the ``empty_since`` dict to a JSON sidecar file at
``{DATA_DIR}/.autoscale_state.json`` (atomic write via temp-file rename).  This
allows the scale-down cooldown to work correctly across separate cron processes
(each ``cspawnctl`` invocation starts a fresh process).  File format::

    {"empty_since": {"swarm3.dojtl.net": "2026-06-26T10:00:00+00:00"}}

Config keys read by the pure layer (all with safe defaults):
  AUTOSCALE_HEADROOM              int   default 2
  AUTOSCALE_ROSTER_FRACTION       float default 0.8
  AUTOSCALE_MAX_ADD_PER_CYCLE     int   default 2
  AUTOSCALE_MAX_REMOVE_PER_CYCLE  int   default 1
  AUTOSCALE_SCALEDOWN_COOLDOWN_MIN int  default 30
  AUTOSCALE_MIN_WORKER_NODES      int   default 1

Additional config keys read by the orchestrator layer:
  AUTOSCALE_ENABLED     bool  default false  — kill-switch; set to "true" to enable
  AUTOSCALE_DRY_RUN     bool  default true   — global dry-run; "false" allows mutations
  DATA_DIR              str   default /tmp   — directory for sidecar state file + lock
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
    "ApplyResult",
    "capacity_for_node",
    "assess_cluster",
    "estimate_demand",
    "compute_deficit",
    "plan_scale_up",
    "plan_scale_down",
    "build_plan",
    # Orchestrator (I/O layer)
    "gather_cluster_state",
    "apply_plan",
    "run_autoscale",
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


@dataclass
class ApplyResult:
    """Result returned by apply_plan and run_autoscale."""
    added: int = 0
    removed: int = 0
    purged: bool = False
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)


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


# ---------------------------------------------------------------------------
# Orchestrator (side-effecting) — Docker, DigitalOcean, and DB I/O only here
# ---------------------------------------------------------------------------
#
# All imports of Docker, DigitalOcean, Flask, and SQLAlchemy models are done
# lazily inside each function body.  This keeps the pure-function section of
# the module importable without those packages (critical for unit-test isolation).
#
# Never call these functions from the pure section above.


def _cfg_bool(cfg, key: str, default: bool) -> bool:
    """Read a boolean config key ('true'/'1'/'yes' → True, else → False)."""
    raw = cfg.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("true", "1", "yes")


def _load_empty_since_sidecar(data_dir: str) -> "dict[str, datetime]":
    """Load the empty_since dict from the JSON sidecar file.

    Returns an empty dict if the file is absent or malformed.
    """
    import json as _json
    from pathlib import Path

    sidecar = Path(data_dir) / ".autoscale_state.json"
    try:
        raw = _json.loads(sidecar.read_text())
        result: dict[str, datetime] = {}
        for fqdn, ts_str in (raw.get("empty_since") or {}).items():
            try:
                dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                result[fqdn] = dt
            except (ValueError, TypeError):
                continue
        return result
    except Exception:
        return {}


def _save_empty_since_sidecar(data_dir: str, empty_since: "dict[str, datetime]") -> None:
    """Atomically write the empty_since dict to the JSON sidecar file."""
    import json as _json
    import os
    from pathlib import Path

    sidecar = Path(data_dir) / ".autoscale_state.json"
    payload = {"empty_since": {fqdn: dt.isoformat() for fqdn, dt in empty_since.items()}}
    tmp = sidecar.with_suffix(".json.tmp")
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        tmp.write_text(_json.dumps(payload, indent=2))
        os.replace(str(tmp), str(sidecar))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def gather_cluster_state(
    app,
    manager_client,
    cfg,
) -> "tuple[list[dict], dict[str, int], int, list[dict], list[dict], dict[str, datetime]]":
    """Read-only snapshot of the current cluster state.

    This is the only function that performs Docker API calls and DB queries for
    the autoscale control loop.  It is strictly read-only: no mutations to the
    DB, Swarm, or DigitalOcean.

    Parameters
    ----------
    app:
        Flask application instance (used for ``app.app_context()``).
    manager_client:
        ``docker.DockerClient`` connected to the swarm manager.
    cfg:
        App config mapping (dict-like).

    Returns
    -------
    tuple of:
        node_dicts       – raw Swarm node attrs dicts from ``manager_client.nodes.list()``
        host_counts      – ``{short_hostname: running_task_count}`` from
                           ``count_hosts_per_node``
        pending_count    – count of CodeHost rows not yet in a ready/mia state
        class_rows       – list of class dicts (fields: running, stops_at, students)
        host_rows        – list of host dicts (fields: is_mia, is_purgeable, app_state,
                           node_name)
        empty_since      – ``{fqdn: datetime_became_empty}`` tracking across cycles
    """
    from cspawn.cli.node import count_hosts_per_node

    # --- Docker reads (no app context needed) ---
    host_counts: dict[str, int] = count_hosts_per_node(manager_client)
    node_dicts: list[dict] = [n.attrs for n in manager_client.nodes.list()]

    # --- DB reads (inside app context) ---
    with app.app_context():
        from cspawn.models import CodeHost, Class

        host_rows: list[dict] = [
            {
                "is_mia": bool(getattr(h, "is_mia", False)),
                "is_purgeable": bool(getattr(h, "is_purgeable", False)),
                "app_state": getattr(h, "app_state", None),
                "node_name": getattr(h, "node_name", None),
            }
            for h in CodeHost.query.all()
        ]

        class_rows: list[dict] = [
            {
                "running": bool(getattr(c, "running", False)),
                "stops_at": getattr(c, "stops_at", None),
                "students": list(getattr(c, "students", []) or []),
            }
            for c in Class.query.filter_by(running=True).all()
        ]

    # Count pending hosts: not yet ready, not MIA
    pending_count = sum(
        1 for h in host_rows
        if h.get("app_state") not in ("ready",) and not h.get("is_mia")
    )

    # --- Build empty_since dict ---
    # Build mapping of short_hostname → fqdn from node_dicts
    short_to_fqdn: dict[str, str] = {}
    for attrs in node_dicts:
        desc = attrs.get("Description") or {}
        hostname = desc.get("Hostname") or ""
        if hostname:
            short = hostname.split(".")[0]
            short_to_fqdn[short] = hostname

    # Load sidecar from previous cycle
    data_dir = cfg.get("DATA_DIR", "/tmp")
    empty_since = _load_empty_since_sidecar(data_dir)

    now = datetime.now(timezone.utc)

    # Update: record first-seen-empty timestamp for newly empty nodes
    for short, fqdn in short_to_fqdn.items():
        count = host_counts.get(short, 0)
        if count == 0:
            if fqdn not in empty_since:
                empty_since[fqdn] = now
        else:
            # Node is no longer empty — remove tracking
            empty_since.pop(fqdn, None)

    # Remove entries for nodes no longer in the cluster
    known_fqdns = set(short_to_fqdn.values())
    for fqdn in list(empty_since.keys()):
        if fqdn not in known_fqdns:
            del empty_since[fqdn]

    return (node_dicts, host_counts, pending_count, class_rows, host_rows, empty_since)


def apply_plan(
    ctx,
    plan: ScalePlan,
    cfg,
    *,
    dry_run: bool,
    app=None,
    manager_client=None,
    mgr=None,
) -> ApplyResult:
    """Execute a ``ScalePlan``: provision new nodes or remove empty ones.

    This is the only function that mutates infrastructure (Docker Swarm /
    DigitalOcean).

    Parameters
    ----------
    ctx:
        Click context passed through to node.py primitives.
    plan:
        The ``ScalePlan`` produced by ``build_plan``.
    cfg:
        App config mapping.
    dry_run:
        When ``True``, log the plan and return immediately without side effects.
    app:
        Flask app for DB access (required for scale-down purge step).
    manager_client:
        Docker client connected to the manager (required for scale-down re-check).
    mgr:
        ``digitalocean.Manager`` instance (required for scale-down).

    Returns
    -------
    ``ApplyResult`` with counts of nodes added/removed and any errors encountered.
    """
    import logging

    log = logging.getLogger("cspawn.autoscale")

    result = ApplyResult(dry_run=dry_run)

    if dry_run:
        log.info("[autoscale] dry-run: %s", plan.summary())
        return result

    errors: list[str] = []

    # --- Scale-up path ---
    if plan.add_large + plan.add_small > 0:
        import click as _click
        from cspawn.cli.node import (
            _create_droplet,
            _configure_node,
            _join_swarm,
            _get_next_serial,
        )
        from cspawn.cs_docker.tiers import load_tiers

        tiers = load_tiers(cfg)
        sorted_tiers = sorted(tiers, key=lambda t: t.capacity)
        tier_small = sorted_tiers[0]
        tier_large = sorted_tiers[-1]

        do_token = cfg.get("DO_TOKEN")
        do_region = cfg.get("DO_REGION") or cfg.get("DO_REGIOIN") or "sfo3"
        do_image = cfg.get("DO_IMAGE", "docker-20-04")
        name_template = cfg.get("DO_NAMES")
        do_tag = cfg.get("DO_TAG")
        project_selector = cfg.get("DO_PROJECT")
        docker_uri = cfg.get("DOCKER_URI", "")

        import digitalocean as _do
        _mgr = mgr or _do.Manager(token=do_token)

        import docker as _docker
        _client = manager_client or _docker.DockerClient(base_url=docker_uri, use_ssh_client=True)

        nodes_to_add: list = (
            [tier_large] * plan.add_large + [tier_small] * plan.add_small
        )

        for tier in nodes_to_add:
            try:
                droplet, ip, fqdn, shortname = _create_droplet(
                    ctx,
                    mgr=_mgr,
                    manager_client=_client,
                    name_template=name_template,
                    do_token=do_token,
                    do_region=do_region,
                    do_size=tier.slug,
                    do_image=do_image,
                    project_selector=project_selector,
                    desired_serial=None,
                    docker_uri=docker_uri,
                    do_tag=do_tag,
                    tier=tier,
                )
                _configure_node(ctx, fqdn, desired_shortname=shortname)
                _join_swarm(ctx, fqdn, _client, docker_uri, tier=tier)
                result.added += 1
                log.info("[autoscale] scale-up: added node %s (tier=%s)", fqdn, tier.name)
            except _click.ClickException as exc:
                msg = f"scale-up error for tier={tier.name}: {exc.format_message()}"
                log.error("[autoscale] %s", msg)
                errors.append(msg)
                # Docker version mismatch or fatal config error — stop adding
                break
            except Exception as exc:
                msg = f"scale-up error for tier={tier.name}: {exc}"
                log.error("[autoscale] %s", msg)
                errors.append(msg)
                break

    # --- Scale-down path ---
    if plan.remove_nodes:
        import click as _click
        from cspawn.cli.node import count_hosts_per_node, graceful_remove_node

        # Purge stale host records first
        if plan.purge_first and app is not None:
            try:
                with app.app_context():
                    app.csm.sync(check_ready=True)
                result.purged = True
                log.info("[autoscale] scale-down: host purge sync completed")
            except Exception as exc:
                log.warning("[autoscale] scale-down: host purge sync failed: %s", exc)

        import digitalocean as _do
        _mgr = mgr or _do.Manager(token=cfg.get("DO_TOKEN"))

        import docker as _docker
        docker_uri = cfg.get("DOCKER_URI", "")
        _client = manager_client or _docker.DockerClient(base_url=docker_uri, use_ssh_client=True)

        for fqdn in plan.remove_nodes:
            short = fqdn.split(".")[0]
            # Re-check emptiness immediately before draining (idempotency / race guard)
            try:
                live_counts = count_hosts_per_node(_client)
                if live_counts.get(short, 0) > 0:
                    log.warning(
                        "[autoscale] scale-down: node %s has running hosts (%d) — skipping",
                        fqdn, live_counts[short],
                    )
                    errors.append(f"node {fqdn} has running hosts — skipped")
                    continue
            except Exception as exc:
                log.warning("[autoscale] scale-down: re-check failed for %s: %s — skipping", fqdn, exc)
                errors.append(f"re-check failed for {fqdn}: {exc}")
                continue

            try:
                graceful_remove_node(ctx, _client, _mgr, fqdn, dry_run=False, log=log)
                result.removed += 1
                log.info("[autoscale] scale-down: removed node %s", fqdn)
            except Exception as exc:
                msg = f"scale-down error for {fqdn}: {exc}"
                log.error("[autoscale] %s", msg)
                errors.append(msg)

    result.errors = errors
    return result


def run_autoscale(
    ctx,
    *,
    dry_run: bool,
    force: bool,
    up_only: "bool | None" = None,
    app=None,
    manager_client=None,
) -> ApplyResult:
    """Single entry point for the autoscale control loop.

    Steps
    -----
    1. Kill-switch check: ``AUTOSCALE_ENABLED`` must be ``true`` to proceed.
    2. Config dry-run override: ``AUTOSCALE_DRY_RUN=true`` forces ``dry_run=True``
       regardless of the ``--dry-run`` CLI flag.
    3. Acquire an exclusive non-blocking file lock (``fcntl.flock``) to prevent
       concurrent cron runs.
    4. Gather cluster state.
    5. Assess cluster → build plan.
    6. Apply ``up_only`` / down-only filter.
    7. Structured log line.
    8. Apply plan.
    9. Persist ``empty_since`` sidecar.
    10. Release lock (in ``finally``).

    Parameters
    ----------
    ctx:
        Click context.
    dry_run:
        Suppress all mutations.  Overridden to ``True`` when ``AUTOSCALE_DRY_RUN``
        config key is ``true``.
    force:
        When ``True``, bypass the scale-down cooldown.
    up_only:
        ``True``  → zero out remove_nodes (scale-up only).
        ``False`` → zero out add counts (scale-down only).
        ``None``  → no filter.
    app:
        Flask app instance.  When ``None``, the function obtains it from
        ``cspawn.init.init_app`` via the CLI context.
    manager_client:
        Docker client connected to the swarm manager.  When ``None``, the
        function creates one from ``DOCKER_URI`` config.

    Returns
    -------
    ``ApplyResult``.
    """
    import fcntl
    import logging
    from pathlib import Path

    log = logging.getLogger("cspawn.autoscale")

    # Lazy-import CLI helpers (avoid circular imports at module level)
    from cspawn.cli.util import get_config as _get_config

    cfg = _get_config()

    # 1. Kill-switch
    if not _cfg_bool(cfg, "AUTOSCALE_ENABLED", False):
        log.info("[autoscale] autoscale disabled (AUTOSCALE_ENABLED=false); exiting")
        return ApplyResult()

    # 2. Config dry-run override
    if _cfg_bool(cfg, "AUTOSCALE_DRY_RUN", True):
        dry_run = True

    data_dir = cfg.get("DATA_DIR", "/tmp")
    lock_path = str(Path(data_dir) / ".autoscale.lock")
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    lock_file = None
    try:
        lock_file = open(lock_path, "w")  # noqa: WPS515
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("[autoscale] previous cycle still running (lock held); aborting")
            return ApplyResult()

        # Resolve app and Docker client if not supplied
        _app = app
        if _app is None:
            from cspawn.cli.util import get_app
            _app = get_app(ctx)

        _manager_client = manager_client
        if _manager_client is None:
            import docker as _docker
            docker_uri = cfg.get("DOCKER_URI", "")
            _manager_client = _docker.DockerClient(base_url=docker_uri, use_ssh_client=True)

        # 4. Gather cluster state
        node_dicts, host_counts, pending_count, class_rows, host_rows, empty_since = (
            gather_cluster_state(_app, _manager_client, cfg)
        )

        # 5. Assess and build plan
        now = datetime.now(timezone.utc)
        state = assess_cluster(node_dicts, host_counts, pending_count, cfg)
        demand = estimate_demand(class_rows, host_rows, cfg)

        # When force=True, bypass cooldown by pretending all empty nodes were empty
        # long enough to satisfy the cooldown.
        effective_empty_since = empty_since
        if force:
            cooldown_min = _cfg_int(cfg, "AUTOSCALE_SCALEDOWN_COOLDOWN_MIN", 30)
            from datetime import timedelta
            effective_empty_since = {
                fqdn: min(ts, now - timedelta(minutes=cooldown_min + 1))
                for fqdn, ts in empty_since.items()
            }

        plan = build_plan(state, demand, cfg, now, effective_empty_since)

        # 6. up_only / down-only filter
        if up_only is True:
            plan.remove_nodes = []
            plan.purge_first = False
        elif up_only is False:
            plan.add_large = 0
            plan.add_small = 0

        # Obtain DO manager for scale-down
        import digitalocean as _do
        do_mgr = _do.Manager(token=cfg.get("DO_TOKEN"))

        # 7. Structured log line
        deficit = max(0, demand - state.total_capacity)
        log.info(
            "[autoscale] demand=%d capacity=%d load=%d deficit=%d excess=%d %s",
            demand,
            state.total_capacity,
            state.total_load,
            deficit,
            state.excess_capacity,
            plan.summary(),
        )

        # 8. Apply plan
        result = apply_plan(
            ctx,
            plan,
            cfg,
            dry_run=dry_run,
            app=_app,
            manager_client=_manager_client,
            mgr=do_mgr,
        )

        # 9. Persist empty_since sidecar
        _save_empty_since_sidecar(data_dir, empty_since)

        return result

    finally:
        # 10. Release lock
        if lock_file is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
            except Exception:
                pass
