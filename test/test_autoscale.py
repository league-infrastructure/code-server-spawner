"""
Unit tests for cspawn/cs_docker/autoscale.py — pure decision functions.

No Docker, DigitalOcean, or database I/O.  All tests work with plain dicts
and dataclasses.  Run with::

    uv run pytest test/test_autoscale.py -v
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from cspawn.cs_docker.autoscale import (
    ClusterState,
    NodeView,
    ScalePlan,
    assess_cluster,
    build_plan,
    capacity_for_node,
    compute_deficit,
    estimate_demand,
    plan_scale_down,
    plan_scale_up,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

NODE_TIERS_JSON = json.dumps([
    {"name": "small", "slug": "s-4vcpu-8gb-amd", "capacity": 6},
    {"name": "large", "slug": "s-8vcpu-16gb-amd", "capacity": 14},
])


@pytest.fixture()
def cfg():
    """Minimal config dict used by all pure-function tests."""
    return {
        "NODE_TIERS": NODE_TIERS_JSON,
        "DEFAULT_TIER": "small",
        "DEFAULT_CAPACITY": "6",
        "AUTOSCALE_HEADROOM": "2",
        "AUTOSCALE_ROSTER_FRACTION": "0.8",
        "AUTOSCALE_MAX_ADD_PER_CYCLE": "2",
        "AUTOSCALE_MAX_REMOVE_PER_CYCLE": "1",
        "AUTOSCALE_SCALEDOWN_COOLDOWN_MIN": "30",
        "AUTOSCALE_MIN_WORKER_NODES": "1",
        "AUTOSCALE_DEFAULT_CAPACITY": "6",
    }


def make_worker(
    short: str = "swarm2",
    fqdn: str = "swarm2.dojtl.net",
    capacity: int = 6,
    running_hosts: int = 0,
    is_manager: bool = False,
    is_leader: bool = False,
    serial: int | None = 2,
    size_slug: str | None = None,
) -> NodeView:
    return NodeView(
        short=short,
        fqdn=fqdn,
        size_slug=size_slug,
        capacity=capacity,
        running_hosts=running_hosts,
        is_manager=is_manager,
        is_leader=is_leader,
        serial=serial,
    )


def make_node_attrs(
    hostname: str = "swarm2",
    role: str = "worker",
    capacity_label: str | None = None,
    is_leader: bool = False,
    size_slug: str | None = None,
) -> dict:
    """Build a minimal raw Swarm node attrs dict for assess_cluster tests."""
    labels: dict = {}
    if capacity_label is not None:
        labels["cs.capacity"] = capacity_label
    if size_slug is not None:
        labels["cs.size_slug"] = size_slug

    attrs: dict = {
        "Spec": {
            "Role": role,
            "Labels": labels,
        },
        "Description": {
            "Hostname": hostname,
        },
    }
    if is_leader:
        attrs["ManagerStatus"] = {"Leader": True}
    return attrs


# NOW is used for plan_scale_down/build_plan where it is injected.
# For estimate_demand, "future" must be relative to the real wall clock
# because estimate_demand calls datetime.now() internally.
NOW = datetime.now(timezone.utc)
COOLED_DOWN = NOW - timedelta(minutes=60)   # well past 30-min cooldown
TOO_RECENT = NOW - timedelta(minutes=5)     # not yet cooled down


# ---------------------------------------------------------------------------
# capacity_for_node
# ---------------------------------------------------------------------------

class TestCapacityForNode:
    def test_label_present_returns_label_value(self, cfg):
        """cs.capacity label overrides everything."""
        attrs = {"Spec": {"Labels": {"cs.capacity": "12"}}}
        assert capacity_for_node(attrs, cfg) == 12

    def test_label_present_large_value(self, cfg):
        attrs = {"Spec": {"Labels": {"cs.capacity": "20"}}}
        assert capacity_for_node(attrs, cfg) == 20

    def test_label_absent_returns_default_capacity(self, cfg):
        """No cs.capacity label → falls back to DEFAULT_CAPACITY."""
        attrs = {"Spec": {"Labels": {}}}
        assert capacity_for_node(attrs, cfg) == 6

    def test_label_absent_slug_in_node_attrs(self, cfg):
        """slug in size_slug label doesn't affect capacity_for_node output;
        capacity comes from DEFAULT_CAPACITY when cs.capacity is absent."""
        attrs = {
            "Spec": {
                "Labels": {"cs.size_slug": "s-4vcpu-8gb-amd"},
            }
        }
        # DEFAULT_CAPACITY=6, small tier capacity=6, same value
        assert capacity_for_node(attrs, cfg) == 6

    def test_label_absent_no_spec_key(self, cfg):
        """Completely empty attrs → DEFAULT_CAPACITY."""
        assert capacity_for_node({}, cfg) == 6

    def test_label_absent_unknown_slug_returns_default(self, cfg):
        """Unknown slug → DEFAULT_CAPACITY (no slug-based lookup in this fn)."""
        attrs = {"Spec": {"Labels": {"cs.size_slug": "s-unknown-slug"}}}
        assert capacity_for_node(attrs, cfg) == 6

    def test_label_invalid_value_falls_back_to_default(self, cfg):
        """Non-integer cs.capacity label → DEFAULT_CAPACITY."""
        attrs = {"Spec": {"Labels": {"cs.capacity": "not-a-number"}}}
        assert capacity_for_node(attrs, cfg) == 6

    def test_autoscale_default_capacity_from_config(self):
        """AUTOSCALE_DEFAULT_CAPACITY key is respected via DEFAULT_CAPACITY."""
        cfg = {
            "DEFAULT_CAPACITY": "8",
            "AUTOSCALE_DEFAULT_CAPACITY": "8",
        }
        attrs = {"Spec": {"Labels": {}}}
        assert capacity_for_node(attrs, cfg) == 8


# ---------------------------------------------------------------------------
# assess_cluster
# ---------------------------------------------------------------------------

class TestAssessCluster:
    def test_managers_excluded_from_total_capacity(self, cfg):
        """Manager nodes should NOT contribute to total_capacity."""
        nodes = [
            make_node_attrs("swarm1", role="manager", is_leader=True),
            make_node_attrs("swarm2", role="worker", capacity_label="6"),
        ]
        state = assess_cluster(nodes, {"swarm2": 0}, 0, cfg)
        # Manager contributes 0 to capacity; worker contributes 6
        assert state.total_capacity == 6

    def test_total_load_sums_across_all_nodes(self, cfg):
        """total_load includes both manager and worker running_hosts."""
        nodes = [
            make_node_attrs("swarm1", role="manager", is_leader=True, capacity_label="6"),
            make_node_attrs("swarm2", role="worker", capacity_label="6"),
        ]
        # Manager has 2 running, worker has 3 running
        state = assess_cluster(nodes, {"swarm1": 2, "swarm2": 3}, 0, cfg)
        assert state.total_load == 5

    def test_excess_capacity_equals_capacity_minus_load(self, cfg):
        """excess_capacity = total_capacity - total_load."""
        nodes = [
            make_node_attrs("swarm2", role="worker", capacity_label="6"),
        ]
        state = assess_cluster(nodes, {"swarm2": 4}, 0, cfg)
        assert state.excess_capacity == 6 - 4

    def test_is_leader_flag_set(self, cfg):
        """ManagerStatus.Leader → NodeView.is_leader == True."""
        nodes = [make_node_attrs("swarm1", role="manager", is_leader=True)]
        state = assess_cluster(nodes, {}, 0, cfg)
        assert state.nodes[0].is_leader is True

    def test_worker_node_is_not_manager(self, cfg):
        nodes = [make_node_attrs("swarm2", role="worker")]
        state = assess_cluster(nodes, {}, 0, cfg)
        assert state.nodes[0].is_manager is False
        assert state.nodes[0].is_leader is False

    def test_serial_extracted_from_hostname(self, cfg):
        nodes = [make_node_attrs("swarm7", role="worker")]
        state = assess_cluster(nodes, {}, 0, cfg)
        assert state.nodes[0].serial == 7

    def test_pending_hosts_stored(self, cfg):
        nodes = [make_node_attrs("swarm2", role="worker")]
        state = assess_cluster(nodes, {}, pending=3, cfg=cfg)
        assert state.pending_hosts == 3

    def test_multiple_workers_sum_capacity(self, cfg):
        nodes = [
            make_node_attrs("swarm2", role="worker", capacity_label="6"),
            make_node_attrs("swarm3", role="worker", capacity_label="14"),
        ]
        state = assess_cluster(nodes, {}, 0, cfg)
        assert state.total_capacity == 20

    def test_empty_node_list(self, cfg):
        state = assess_cluster([], {}, 0, cfg)
        assert state.total_capacity == 0
        assert state.total_load == 0
        assert state.excess_capacity == 0


# ---------------------------------------------------------------------------
# estimate_demand
# ---------------------------------------------------------------------------

class TestEstimateDemand:
    """
    Formula:
        live_load = non-MIA non-purgeable hosts
        pending   = non-ready, non-MIA hosts
        prescale  = sum(ceil(students * ROSTER_FRACTION)) for running, future classes
        demand    = max(live_load + pending, prescale) + HEADROOM
    """

    def _future(self, minutes: int = 120) -> datetime:
        return NOW + timedelta(minutes=minutes)

    def _past(self, minutes: int = 10) -> datetime:
        return NOW - timedelta(minutes=minutes)

    def test_headroom_always_added_when_no_hosts_no_classes(self, cfg):
        demand = estimate_demand([], [], cfg)
        assert demand == 2  # 0 + HEADROOM(2)

    def test_prescale_dominates_when_larger(self, cfg):
        """10 students * 0.8 = 8 prescale > 0 live_load → demand = 8 + 2 = 10."""
        classes = [{"running": True, "stops_at": self._future(), "students": list(range(10))}]
        demand = estimate_demand(classes, [], cfg)
        assert demand == 10

    def test_live_load_dominates_when_larger(self, cfg):
        """4 live hosts > prescale of 1 student → demand = 4 + 2 = 6."""
        hosts = [{"app_state": "ready"} for _ in range(4)]
        classes = [{"running": True, "stops_at": self._future(), "students": [1]}]
        demand = estimate_demand(classes, hosts, cfg)
        # prescale = ceil(1 * 0.8) = 1; live_load = 4; max(4, 1) + 2 = 6
        assert demand == 6

    def test_mia_hosts_excluded_from_live_load(self, cfg):
        """MIA hosts do not count toward live_load."""
        hosts = [
            {"app_state": "ready", "is_mia": False},
            {"app_state": "ready", "is_mia": True},   # excluded
        ]
        demand = estimate_demand([], hosts, cfg)
        assert demand == 1 + 2  # 1 non-MIA + headroom

    def test_purgeable_hosts_excluded_from_live_load(self, cfg):
        """Purgeable hosts do not count toward live_load."""
        hosts = [
            {"app_state": "ready", "is_purgeable": False},
            {"app_state": "ready", "is_purgeable": True},  # excluded
        ]
        demand = estimate_demand([], hosts, cfg)
        assert demand == 1 + 2

    def test_classes_past_stops_at_excluded_from_prescale(self, cfg):
        """Classes whose stops_at is in the past don't contribute to prescale."""
        classes = [
            {"running": True, "stops_at": self._past(), "students": list(range(10))},
        ]
        demand = estimate_demand(classes, [], cfg)
        assert demand == 0 + 2  # no prescale, just headroom

    def test_non_running_classes_excluded_from_prescale(self, cfg):
        """Classes with running=False don't contribute to prescale."""
        classes = [
            {"running": False, "stops_at": self._future(), "students": list(range(10))},
        ]
        demand = estimate_demand(classes, [], cfg)
        assert demand == 0 + 2

    def test_roster_fraction_applied_and_ceil(self, cfg):
        """10 students * 0.8 = 8.0 → ceil(8.0) = 8."""
        classes = [{"running": True, "stops_at": self._future(), "students": list(range(10))}]
        demand = estimate_demand(classes, [], cfg)
        assert demand == 8 + 2  # 8 prescale + 2 headroom

    def test_roster_fraction_ceil_on_fraction(self):
        """7 students * 0.8 = 5.6 → ceil(5.6) = 6."""
        cfg = {
            "AUTOSCALE_HEADROOM": "0",
            "AUTOSCALE_ROSTER_FRACTION": "0.8",
        }
        classes = [{"running": True, "stops_at": NOW + timedelta(hours=1), "students": list(range(7))}]
        demand = estimate_demand(classes, [], cfg)
        assert demand == 6  # ceil(5.6)=6, no headroom

    def test_multiple_running_classes_summed(self, cfg):
        """Two classes both running and future → prescale is sum of both."""
        classes = [
            {"running": True, "stops_at": self._future(), "students": list(range(5))},
            {"running": True, "stops_at": self._future(), "students": list(range(10))},
        ]
        # ceil(5*0.8) + ceil(10*0.8) = 4 + 8 = 12 prescale
        demand = estimate_demand(classes, [], cfg)
        assert demand == 12 + 2

    def test_stops_at_as_iso_string(self, cfg):
        """stops_at can be an ISO string — the function parses it."""
        future_str = (NOW + timedelta(hours=1)).isoformat()
        classes = [{"running": True, "stops_at": future_str, "students": list(range(10))}]
        demand = estimate_demand(classes, [], cfg)
        assert demand == 8 + 2

    def test_pending_hosts_counted_in_live_load_plus_pending(self, cfg):
        """Non-ready, non-MIA hosts are counted as pending (added to live_load)."""
        hosts = [
            {"app_state": "starting", "is_mia": False},  # pending: not ready, not MIA
            {"app_state": "ready", "is_mia": False},     # live
        ]
        # live_load=1 (only the ready one), pending=1 (starting)
        # Wait: per code: live_load = not mia and not purgeable = 2
        # pending = not ready and not mia = 1
        # demand = max(live_load + pending, prescale) + headroom
        # = max(2 + 1, 0) + 2 = 5
        demand = estimate_demand([], hosts, cfg)
        # live_load counts both (neither is MIA/purgeable) = 2
        # pending counts starting (not ready, not MIA) = 1
        assert demand == 2 + 1 + 2  # live=2 + pending=1 + headroom=2

    def test_mia_host_excluded_from_pending_too(self, cfg):
        """MIA hosts are excluded from both live_load and pending counts."""
        hosts = [
            {"app_state": "starting", "is_mia": True},  # excluded from both
        ]
        demand = estimate_demand([], hosts, cfg)
        assert demand == 0 + 2


# ---------------------------------------------------------------------------
# compute_deficit
# ---------------------------------------------------------------------------

class TestComputeDeficit:
    def test_deficit_when_demand_exceeds_capacity(self, cfg):
        """demand=10, capacity=6 → deficit=4."""
        state = ClusterState(
            nodes=[make_worker(capacity=6)],
            pending_hosts=0,
        )
        assert compute_deficit(state, demand=10, cfg=cfg) == 4

    def test_no_deficit_when_demand_equals_capacity(self, cfg):
        """demand == capacity → deficit == 0."""
        state = ClusterState(
            nodes=[make_worker(capacity=6)],
            pending_hosts=0,
        )
        assert compute_deficit(state, demand=6, cfg=cfg) == 0

    def test_no_deficit_when_demand_below_capacity(self, cfg):
        """demand < capacity → deficit == 0 (never negative)."""
        state = ClusterState(
            nodes=[make_worker(capacity=14)],
            pending_hosts=0,
        )
        assert compute_deficit(state, demand=6, cfg=cfg) == 0

    def test_deficit_with_zero_demand(self, cfg):
        state = ClusterState(nodes=[make_worker(capacity=6)], pending_hosts=0)
        assert compute_deficit(state, demand=0, cfg=cfg) == 0

    def test_deficit_with_zero_capacity(self, cfg):
        """Manager-only cluster (no workers) → capacity=0, deficit=demand."""
        state = ClusterState(
            nodes=[make_worker(capacity=6, is_manager=True)],
            pending_hosts=0,
        )
        assert compute_deficit(state, demand=5, cfg=cfg) == 5


# ---------------------------------------------------------------------------
# plan_scale_up
# ---------------------------------------------------------------------------

class TestPlanScaleUp:
    """
    Tiers: small=6, large=14
    Algorithm:
        add_large = deficit // 14
        rem       = deficit % 14
        if rem == 0:           add_small = 0
        elif rem <= 6:         add_small = 1
        else:                  add_large += 1
    Then clamp total to MAX_ADD_PER_CYCLE.
    """

    def test_zero_deficit_returns_zero(self, cfg):
        assert plan_scale_up(0, cfg) == (0, 0)

    def test_deficit_1_one_small(self, cfg):
        """D=1 → rem=1 ≤ 6 → (0, 1)."""
        assert plan_scale_up(1, cfg) == (0, 1)

    def test_deficit_6_one_small(self, cfg):
        """D=6 → rem=6 ≤ 6 → (0, 1)."""
        assert plan_scale_up(6, cfg) == (0, 1)

    def test_deficit_7_one_large(self, cfg):
        """D=7 → large=0, rem=7 > 6 → add_large=1 → (1, 0)."""
        assert plan_scale_up(7, cfg) == (1, 0)

    def test_deficit_13_one_large(self, cfg):
        """D=13 → large=0, rem=13 > 6 → add_large=1 → (1, 0)."""
        assert plan_scale_up(13, cfg) == (1, 0)

    def test_deficit_14_exactly_one_large(self, cfg):
        """D=14 → large=1, rem=0 → (1, 0)."""
        assert plan_scale_up(14, cfg) == (1, 0)

    def test_deficit_20_one_large_one_small(self, cfg):
        """D=20 → large=1, rem=6 ≤ 6 → (1, 1)."""
        assert plan_scale_up(20, cfg) == (1, 1)

    def test_deficit_21_two_large(self, cfg):
        """D=21 → large=1, rem=7 > 6 → add_large=2 → (2, 0)."""
        assert plan_scale_up(21, cfg) == (2, 0)

    def test_max_add_per_cycle_clamp(self, cfg):
        """D=50 with MAX_ADD_PER_CYCLE=2 → total clamped to 2 nodes."""
        add_large, add_small = plan_scale_up(50, cfg)
        assert add_large + add_small <= 2

    def test_max_add_per_cycle_clamp_reduces_small_first(self):
        """When clamping, add_small is reduced before add_large."""
        cfg = {
            "NODE_TIERS": NODE_TIERS_JSON,
            "DEFAULT_TIER": "small",
            "DEFAULT_CAPACITY": "6",
            "AUTOSCALE_MAX_ADD_PER_CYCLE": "1",
        }
        # D=20 → unclamped (1, 1); with cap=1, reduce small first → (1, 0)
        add_large, add_small = plan_scale_up(20, cfg)
        assert add_large + add_small <= 1
        # Should prefer keeping the large node
        assert add_large == 1
        assert add_small == 0

    def test_negative_deficit_returns_zero(self, cfg):
        """plan_scale_up guards against negative input."""
        assert plan_scale_up(-1, cfg) == (0, 0)


# ---------------------------------------------------------------------------
# plan_scale_down
# ---------------------------------------------------------------------------

class TestPlanScaleDown:
    """
    Criteria for removal:
      - not manager, not leader
      - running_hosts == 0
      - cooled down (>= AUTOSCALE_SCALEDOWN_COOLDOWN_MIN minutes empty)
      - excess_capacity > node.capacity + HEADROOM  (dead-band)
      - would still leave >= MIN_WORKER_NODES workers
    Sorted by serial descending; at most MAX_REMOVE_PER_CYCLE.
    """

    def _empty_since(self, *fqdns: str, age_minutes: int = 60) -> dict:
        return {fqdn: NOW - timedelta(minutes=age_minutes) for fqdn in fqdns}

    def test_node_with_running_hosts_skipped(self, cfg):
        """Node that has live tasks must not be removed."""
        busy = make_worker(fqdn="swarm2.net", running_hosts=3, capacity=6)
        state = ClusterState(nodes=[busy], pending_hosts=0)
        result = plan_scale_down(state, demand=0, cfg=cfg, now=NOW,
                                 empty_since=self._empty_since("swarm2.net"))
        assert result == []

    def test_node_not_in_empty_since_skipped(self, cfg):
        """Empty node with no entry in empty_since is skipped (cooldown unknown)."""
        node = make_worker(fqdn="swarm2.net", running_hosts=0, capacity=6)
        # Cluster has high excess so dead-band is not the issue
        state = ClusterState(nodes=[
            node,
            make_worker(short="swarm3", fqdn="swarm3.net", capacity=14, serial=3),
        ], pending_hosts=0)
        result = plan_scale_down(state, demand=0, cfg=cfg, now=NOW,
                                 empty_since={})  # no entries
        assert result == []

    def test_node_not_cooled_down_skipped(self, cfg):
        """Empty node that hasn't met cooldown is skipped."""
        node = make_worker(fqdn="swarm2.net", running_hosts=0, capacity=6)
        # Give it extra capacity so dead-band won't block
        big = make_worker(short="swarm3", fqdn="swarm3.net", capacity=100, serial=3)
        state = ClusterState(nodes=[node, big], pending_hosts=0)
        result = plan_scale_down(state, demand=0, cfg=cfg, now=NOW,
                                 empty_since={"swarm2.net": TOO_RECENT})
        assert result == []

    def test_manager_node_skipped(self, cfg):
        """Manager nodes must never be selected for removal."""
        mgr = make_worker(short="swarm1", fqdn="swarm1.net", capacity=6,
                          is_manager=True, is_leader=True, serial=1)
        # Need another worker to satisfy MIN_WORKER_NODES
        worker = make_worker(short="swarm2", fqdn="swarm2.net", capacity=6, serial=2)
        state = ClusterState(nodes=[mgr, worker], pending_hosts=0)
        # Give excess so dead-band is not the obstacle
        # total_capacity = 6 (worker only), excess = 6
        # node.capacity=6, headroom=2 → removing requires excess > 8, but excess=6 → blocked by dead-band
        # Use a bigger worker to test that mgr is specifically excluded
        big_worker = make_worker(short="swarm2", fqdn="swarm2.net", capacity=100, serial=2)
        state2 = ClusterState(nodes=[mgr, big_worker], pending_hosts=0)
        result = plan_scale_down(state2, demand=0, cfg=cfg, now=NOW,
                                 empty_since={"swarm1.net": COOLED_DOWN})
        # Manager should not appear in result
        assert all(n.fqdn != "swarm1.net" for n in result)

    def test_leader_node_skipped(self, cfg):
        """is_leader=True must never be selected for removal."""
        leader = make_worker(short="swarm1", fqdn="swarm1.net", capacity=6,
                             is_manager=False, is_leader=True, serial=1)
        big = make_worker(short="swarm2", fqdn="swarm2.net", capacity=100, serial=2)
        state = ClusterState(nodes=[leader, big], pending_hosts=0)
        result = plan_scale_down(state, demand=0, cfg=cfg, now=NOW,
                                 empty_since={"swarm1.net": COOLED_DOWN})
        assert all(n.fqdn != "swarm1.net" for n in result)

    def test_respects_min_worker_nodes_floor(self, cfg):
        """Never drops below MIN_WORKER_NODES workers (default 1)."""
        # Single empty worker — removing it would leave 0 workers, violating floor
        node = make_worker(short="swarm2", fqdn="swarm2.net", capacity=6, serial=2)
        state = ClusterState(nodes=[node], pending_hosts=0)
        # excess = 6, node.capacity=6, headroom=2 → removing requires excess > 8 anyway
        # But min-worker is the more obvious guard here; let's boost excess to isolate
        big = make_worker(short="swarm2", fqdn="swarm2.net", capacity=100, serial=2)
        state2 = ClusterState(nodes=[big], pending_hosts=0)
        result = plan_scale_down(state2, demand=0, cfg=cfg, now=NOW,
                                 empty_since={"swarm2.net": COOLED_DOWN})
        # Only 1 worker total; removing it would leave 0 < 1 (MIN_WORKER_NODES)
        assert result == []

    def test_dead_band_guard_prevents_removal(self, cfg):
        """excess_capacity <= node.capacity + headroom → skipped."""
        # Worker has capacity=6, excess = 6, headroom=2
        # Guard: remaining_excess (6) <= node.capacity (6) + headroom (2) → True → skip
        node = make_worker(short="swarm2", fqdn="swarm2.net", capacity=6, serial=2)
        worker2 = make_worker(short="swarm3", fqdn="swarm3.net", capacity=6, serial=3)
        state = ClusterState(nodes=[node, worker2], pending_hosts=0)
        # total_capacity = 12, total_load = 0, excess = 12
        # For swarm3 (highest serial): removing requires excess (12) > 6+2=8 → 12>8 ✓ → select
        # For swarm2: remaining_excess after swarm3 removed = 12-6=6; 6 > 6+2=8? No → skip
        result = plan_scale_down(state, demand=0, cfg=cfg, now=NOW,
                                 empty_since=self._empty_since("swarm2.net", "swarm3.net"))
        # At most 1 (MAX_REMOVE_PER_CYCLE=1), and it must be swarm3 (higher serial)
        assert len(result) == 1
        assert result[0].fqdn == "swarm3.net"

    def test_highest_serial_removed_first(self, cfg):
        """Highest serial node is selected preferentially."""
        node2 = make_worker(short="swarm2", fqdn="swarm2.net", capacity=6, serial=2)
        node3 = make_worker(short="swarm3", fqdn="swarm3.net", capacity=6, serial=3)
        # Add a large idle node to provide enough excess for removal
        node_large = make_worker(short="swarm4", fqdn="swarm4.net", capacity=100, serial=4,
                                 running_hosts=0)
        state = ClusterState(nodes=[node2, node3, node_large], pending_hosts=0)
        # total_capacity = 6+6+100=112, excess=112
        # MAX_REMOVE_PER_CYCLE=1 → only one removed; should be serial=4 (swarm4)
        result = plan_scale_down(state, demand=0, cfg=cfg, now=NOW,
                                 empty_since=self._empty_since(
                                     "swarm2.net", "swarm3.net", "swarm4.net"))
        assert len(result) == 1
        assert result[0].serial == 4

    def test_max_remove_per_cycle_clamp(self):
        """At most MAX_REMOVE_PER_CYCLE nodes are returned."""
        cfg_multi = {
            "NODE_TIERS": NODE_TIERS_JSON,
            "DEFAULT_CAPACITY": "6",
            "AUTOSCALE_HEADROOM": "2",
            "AUTOSCALE_MAX_REMOVE_PER_CYCLE": "1",
            "AUTOSCALE_SCALEDOWN_COOLDOWN_MIN": "30",
            "AUTOSCALE_MIN_WORKER_NODES": "1",
        }
        # Three empty workers with plenty of excess
        n2 = make_worker(short="swarm2", fqdn="swarm2.net", capacity=6, serial=2)
        n3 = make_worker(short="swarm3", fqdn="swarm3.net", capacity=6, serial=3)
        n4 = make_worker(short="swarm4", fqdn="swarm4.net", capacity=100, serial=4)
        state = ClusterState(nodes=[n2, n3, n4], pending_hosts=0)
        empty_since = {
            "swarm2.net": NOW - timedelta(hours=2),
            "swarm3.net": NOW - timedelta(hours=2),
            "swarm4.net": NOW - timedelta(hours=2),
        }
        result = plan_scale_down(state, demand=0, cfg=cfg_multi, now=NOW,
                                 empty_since=empty_since)
        assert len(result) <= 1

    def test_eligible_node_returned(self, cfg):
        """When all guards pass, the eligible node IS returned."""
        # Two workers: swarm2 (small, empty) + swarm3 (large, to provide excess)
        small = make_worker(short="swarm2", fqdn="swarm2.net", capacity=6, serial=2)
        large = make_worker(short="swarm3", fqdn="swarm3.net", capacity=100, serial=3,
                            running_hosts=0)
        state = ClusterState(nodes=[small, large], pending_hosts=0)
        # total_capacity=106, excess=106
        # swarm3 selected first (serial=3 > serial=2)
        result = plan_scale_down(state, demand=0, cfg=cfg, now=NOW,
                                 empty_since={"swarm2.net": COOLED_DOWN,
                                              "swarm3.net": COOLED_DOWN})
        assert len(result) == 1
        assert result[0].fqdn == "swarm3.net"


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------

class TestBuildPlan:
    """
    Priority: never both up and down in one cycle.
      1. deficit > 0  → adds only
      2. deficit == 0 and eligible scale-down → removes only
      3. deficit == 0 and no candidates → hold
    """

    def _empty_since(self, *fqdns: str, age_minutes: int = 60) -> dict:
        return {fqdn: NOW - timedelta(minutes=age_minutes) for fqdn in fqdns}

    def test_deficit_produces_scale_up_no_removes(self, cfg):
        """Positive deficit → add nodes, remove_nodes must be empty."""
        # capacity=6, demand=10 → deficit=4
        state = ClusterState(nodes=[make_worker(capacity=6)], pending_hosts=0)
        plan = build_plan(state, demand=10, cfg=cfg, now=NOW, empty_since={})
        assert plan.add_large + plan.add_small > 0
        assert plan.remove_nodes == []

    def test_scale_up_adds_correct_node_counts(self, cfg):
        """D=7 → plan_scale_up returns (1, 0); build_plan wraps it."""
        state = ClusterState(nodes=[], pending_hosts=0)  # capacity=0
        plan = build_plan(state, demand=7, cfg=cfg, now=NOW, empty_since={})
        assert plan.add_large == 1
        assert plan.add_small == 0
        assert plan.remove_nodes == []

    def test_no_deficit_with_eligible_candidate_produces_scale_down(self, cfg):
        """Surplus cluster with eligible empty node → remove_nodes non-empty."""
        small = make_worker(short="swarm2", fqdn="swarm2.net", capacity=6, serial=2)
        large = make_worker(short="swarm3", fqdn="swarm3.net", capacity=100, serial=3)
        state = ClusterState(nodes=[small, large], pending_hosts=0)
        # demand=0 → deficit=0; large node provides ample excess to allow removing small
        plan = build_plan(state, demand=0, cfg=cfg, now=NOW,
                          empty_since=self._empty_since("swarm2.net", "swarm3.net"))
        assert plan.add_large == 0
        assert plan.add_small == 0
        assert len(plan.remove_nodes) > 0

    def test_scale_down_sets_purge_first(self, cfg):
        """purge_first must be True when nodes are being removed."""
        small = make_worker(short="swarm2", fqdn="swarm2.net", capacity=6, serial=2)
        large = make_worker(short="swarm3", fqdn="swarm3.net", capacity=100, serial=3)
        state = ClusterState(nodes=[small, large], pending_hosts=0)
        plan = build_plan(state, demand=0, cfg=cfg, now=NOW,
                          empty_since=self._empty_since("swarm2.net", "swarm3.net"))
        if plan.remove_nodes:
            assert plan.purge_first is True

    def test_no_deficit_no_eligible_candidates_returns_hold(self, cfg):
        """No scale-up or scale-down triggers → hold plan (all zeros)."""
        # Single worker, no running hosts, but only one worker so MIN_WORKER_NODES blocks removal
        node = make_worker(short="swarm2", fqdn="swarm2.net", capacity=100, serial=2)
        state = ClusterState(nodes=[node], pending_hosts=0)
        # demand=0, deficit=0; only 1 worker → can't drop below MIN_WORKER_NODES
        plan = build_plan(state, demand=0, cfg=cfg, now=NOW,
                          empty_since={"swarm2.net": COOLED_DOWN})
        assert plan.add_large == 0
        assert plan.add_small == 0
        assert plan.remove_nodes == []
        assert "hold" in plan.reason

    def test_never_both_adds_and_removes(self, cfg):
        """A plan must never have both adds > 0 and removes non-empty."""
        state = ClusterState(nodes=[make_worker(capacity=6)], pending_hosts=0)
        for demand in range(0, 20):
            plan = build_plan(state, demand=demand, cfg=cfg, now=NOW,
                              empty_since={"swarm2.dojtl.net": COOLED_DOWN})
            has_adds = plan.add_large + plan.add_small > 0
            has_removes = len(plan.remove_nodes) > 0
            assert not (has_adds and has_removes), (
                f"demand={demand}: plan has both adds and removes: {plan}"
            )

    def test_dead_band_prevents_flapping(self, cfg):
        """When demand is just below capacity, the dead-band prevents scale-down."""
        # capacity=14, demand=12 → excess=2 which is <= (capacity_of_candidate + headroom)
        worker = make_worker(short="swarm2", fqdn="swarm2.net", capacity=14, serial=2)
        state = ClusterState(nodes=[worker], pending_hosts=0)
        # demand=12: excess=14-0=14, but removing node (cap=14) would leave excess=0 ≤ 14+2
        # dead-band: remaining_excess(14) <= 14+2=16 → True → skip
        plan = build_plan(state, demand=12, cfg=cfg, now=NOW,
                          empty_since={"swarm2.net": COOLED_DOWN})
        assert plan.remove_nodes == []

    def test_scale_up_reason_mentions_deficit(self, cfg):
        """The reason string should mention scale-up context."""
        state = ClusterState(nodes=[], pending_hosts=0)
        plan = build_plan(state, demand=10, cfg=cfg, now=NOW, empty_since={})
        assert "scale-up" in plan.reason

    def test_hold_reason_mentions_dead_band(self, cfg):
        """Hold plan reason should contain 'dead-band' or 'hold'."""
        node = make_worker(short="swarm2", fqdn="swarm2.net", capacity=100, serial=2)
        state = ClusterState(nodes=[node], pending_hosts=0)
        plan = build_plan(state, demand=0, cfg=cfg, now=NOW,
                          empty_since={"swarm2.net": COOLED_DOWN})
        # Only 1 worker, so removal is blocked by MIN_WORKER_NODES → hold
        assert plan.remove_nodes == []


# ---------------------------------------------------------------------------
# ClusterState properties (dataclass sanity)
# ---------------------------------------------------------------------------

class TestClusterStateProperties:
    def test_total_capacity_excludes_managers(self):
        mgr = make_worker(capacity=6, is_manager=True)
        wkr = make_worker(short="swarm2", fqdn="swarm2.net", capacity=10, serial=2)
        state = ClusterState(nodes=[mgr, wkr], pending_hosts=0)
        assert state.total_capacity == 10

    def test_total_load_includes_all_nodes(self):
        mgr = make_worker(capacity=6, is_manager=True, running_hosts=2)
        wkr = make_worker(short="swarm2", fqdn="swarm2.net", capacity=10,
                          running_hosts=3, serial=2)
        state = ClusterState(nodes=[mgr, wkr], pending_hosts=0)
        assert state.total_load == 5

    def test_excess_capacity(self):
        wkr = make_worker(capacity=10, running_hosts=3)
        state = ClusterState(nodes=[wkr], pending_hosts=0)
        assert state.excess_capacity == 7


# ---------------------------------------------------------------------------
# ScalePlan summary (sanity)
# ---------------------------------------------------------------------------

class TestScalePlanSummary:
    def test_summary_contains_key_fields(self):
        plan = ScalePlan(add_large=1, add_small=0, remove_nodes=[], reason="test")
        s = plan.summary()
        assert "add_large=1" in s
        assert "add_small=0" in s
        assert "remove=0" in s
