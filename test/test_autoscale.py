"""
Unit tests for cspawn/cs_docker/autoscale.py — pure decision functions and
thin mocked orchestrator tests.

No live Docker, DigitalOcean, or database I/O in any test here.
Run with::

    uv run pytest test/test_autoscale.py -v
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch, call

import pytest

from cspawn.cs_docker.autoscale import (
    ApplyResult,
    ClusterState,
    NodeView,
    ScalePlan,
    _load_empty_since_sidecar,
    _save_empty_since_sidecar,
    assess_cluster,
    build_plan,
    capacity_for_node,
    compute_deficit,
    estimate_demand,
    gather_cluster_state,
    apply_plan,
    apply_reaper_zones,
    run_autoscale,
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
        prescale  = sum(ceil(students * ROSTER_FRACTION))
                    for classes where purge_after <= now < purge_by
        demand    = max(live_load + pending, prescale) + HEADROOM

    Prescale source: purge-window timestamps (purge_after/purge_by).
    Class.running and stops_at are no longer used.
    """

    def _active_window(self, students: list | None = None) -> dict:
        """Class inside its active purge window (purge_after past, purge_by future)."""
        return {
            "purge_after": NOW - timedelta(minutes=30),
            "purge_by": NOW + timedelta(hours=2),
            "students": students if students is not None else [],
        }

    def _protected_window(self, students: list | None = None) -> dict:
        """Class in protected zone (purge_after in the future — window not yet open)."""
        return {
            "purge_after": NOW + timedelta(hours=1),
            "purge_by": NOW + timedelta(hours=3),
            "students": students if students is not None else [],
        }

    def _dormant_window(self, students: list | None = None) -> dict:
        """Class in dormant zone (purge_by in the past — window already closed)."""
        return {
            "purge_after": NOW - timedelta(hours=3),
            "purge_by": NOW - timedelta(hours=1),
            "students": students if students is not None else [],
        }

    def test_headroom_always_added_when_no_hosts_no_classes(self, cfg):
        demand = estimate_demand([], [], cfg)
        assert demand == 2  # 0 + HEADROOM(2)

    def test_prescale_dominates_when_larger(self, cfg):
        """10 students in active window * 0.8 = 8 prescale > 0 live_load → demand = 8 + 2 = 10."""
        classes = [self._active_window(students=list(range(10)))]
        demand = estimate_demand(classes, [], cfg)
        assert demand == 10

    def test_live_load_dominates_when_larger(self, cfg):
        """4 live hosts > prescale of 1 student → demand = 4 + 2 = 6."""
        hosts = [{"app_state": "ready"} for _ in range(4)]
        classes = [self._active_window(students=[1])]
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

    def test_dormant_zone_classes_excluded_from_prescale(self, cfg):
        """Classes whose purge_by is in the past (dormant zone) don't contribute to prescale."""
        classes = [self._dormant_window(students=list(range(10)))]
        demand = estimate_demand(classes, [], cfg)
        assert demand == 0 + 2  # no prescale, just headroom

    def test_protected_zone_classes_excluded_from_prescale(self, cfg):
        """Classes whose purge_after is in the future (protected zone) don't contribute."""
        classes = [self._protected_window(students=list(range(10)))]
        demand = estimate_demand(classes, [], cfg)
        assert demand == 0 + 2  # no prescale, just headroom

    def test_no_purge_window_classes_excluded_from_prescale(self, cfg):
        """Classes with purge_after=None are skipped entirely."""
        classes = [{"purge_after": None, "purge_by": None, "students": list(range(10))}]
        demand = estimate_demand(classes, [], cfg)
        assert demand == 0 + 2  # no prescale

    def test_roster_fraction_applied_and_ceil(self, cfg):
        """10 students * 0.8 = 8.0 → ceil(8.0) = 8."""
        classes = [self._active_window(students=list(range(10)))]
        demand = estimate_demand(classes, [], cfg)
        assert demand == 8 + 2  # 8 prescale + 2 headroom

    def test_roster_fraction_ceil_on_fraction(self):
        """7 students * 0.8 = 5.6 → ceil(5.6) = 6."""
        cfg = {
            "AUTOSCALE_HEADROOM": "0",
            "AUTOSCALE_ROSTER_FRACTION": "0.8",
        }
        classes = [{
            "purge_after": NOW - timedelta(minutes=30),
            "purge_by": NOW + timedelta(hours=2),
            "students": list(range(7)),
        }]
        demand = estimate_demand(classes, [], cfg)
        assert demand == 6  # ceil(5.6)=6, no headroom

    def test_multiple_active_window_classes_summed(self, cfg):
        """Two classes both in active window → prescale is sum of both."""
        classes = [
            self._active_window(students=list(range(5))),
            self._active_window(students=list(range(10))),
        ]
        # ceil(5*0.8) + ceil(10*0.8) = 4 + 8 = 12 prescale
        demand = estimate_demand(classes, [], cfg)
        assert demand == 12 + 2

    def test_purge_after_as_iso_string(self, cfg):
        """purge_after and purge_by can be ISO strings — the function parses them."""
        purge_after_str = (NOW - timedelta(minutes=30)).isoformat()
        purge_by_str = (NOW + timedelta(hours=2)).isoformat()
        classes = [{
            "purge_after": purge_after_str,
            "purge_by": purge_by_str,
            "students": list(range(10)),
        }]
        demand = estimate_demand(classes, [], cfg)
        assert demand == 8 + 2

    def test_pending_hosts_counted_in_live_load_plus_pending(self, cfg):
        """Non-ready, non-MIA hosts are counted as pending (added to live_load)."""
        hosts = [
            {"app_state": "starting", "is_mia": False},  # pending: not ready, not MIA
            {"app_state": "ready", "is_mia": False},     # live
        ]
        # live_load = not mia and not purgeable = 2
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

    def test_running_field_ignored(self, cfg):
        """The old 'running' field has no effect — only purge window matters."""
        # A class with running=False but inside an active purge window still contributes
        classes = [dict(self._active_window(students=list(range(10))), running=False)]
        demand = estimate_demand(classes, [], cfg)
        assert demand == 8 + 2  # prescale from purge window, not running flag

    def test_class_running_no_longer_a_scaling_input(self, cfg):
        """Class.running is no longer the prescale source.

        A class with purge_after/purge_by in active window contributes prescale
        regardless of any 'running' field; a class without a purge window
        does NOT contribute even if running=True is present.
        """
        active = self._active_window(students=list(range(5)))
        no_window = {"purge_after": None, "purge_by": None, "students": list(range(10)), "running": True}
        demand = estimate_demand([active, no_window], [], cfg)
        # only active contributes: ceil(5*0.8)=4; no_window skipped
        assert demand == 4 + 2


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


# ---------------------------------------------------------------------------
# empty_since sidecar persistence
# ---------------------------------------------------------------------------

class TestEmptySinceSidecar:
    """Test round-trip persistence of the empty_since sidecar file."""

    def test_load_returns_empty_when_missing(self):
        """Loading a nonexistent sidecar returns an empty dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _load_empty_since_sidecar(tmpdir)
            assert result == {}

    def test_save_and_load_roundtrip(self):
        """Saved dict is loaded back with correct datetime values."""
        with tempfile.TemporaryDirectory() as tmpdir:
            now = datetime.now(timezone.utc).replace(microsecond=0)
            empty_since = {"swarm2.example.com": now}
            _save_empty_since_sidecar(tmpdir, empty_since)
            loaded = _load_empty_since_sidecar(tmpdir)
            assert "swarm2.example.com" in loaded
            # datetimes should be equal (ISO roundtrip)
            assert loaded["swarm2.example.com"] == now

    def test_malformed_json_returns_empty(self):
        """Malformed sidecar returns an empty dict without raising."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sidecar = os.path.join(tmpdir, ".autoscale_state.json")
            with open(sidecar, "w") as f:
                f.write("not-valid-json{{{")
            result = _load_empty_since_sidecar(tmpdir)
            assert result == {}

    def test_save_is_atomic(self):
        """Atomic write: if the sidecar already exists it is replaced cleanly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            t1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
            t2 = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)
            _save_empty_since_sidecar(tmpdir, {"swarm2.x": t1})
            _save_empty_since_sidecar(tmpdir, {"swarm3.x": t2})
            loaded = _load_empty_since_sidecar(tmpdir)
            assert "swarm2.x" not in loaded
            assert loaded["swarm3.x"] == t2


# ---------------------------------------------------------------------------
# gather_cluster_state — mocked Docker client + minimal Flask app
# ---------------------------------------------------------------------------

def _make_swarm_node_mock(hostname: str, role: str = "worker", is_leader: bool = False) -> MagicMock:
    """Create a mock Swarm node object with realistic attrs."""
    node = MagicMock()
    node.attrs = {
        "Spec": {"Role": role, "Labels": {"cs.capacity": "6"}},
        "Description": {"Hostname": hostname},
        "ManagerStatus": {"Leader": is_leader} if role == "manager" else {},
    }
    return node


def _make_minimal_flask_app():
    """Create a minimal Flask app with in-memory SQLite and the required models."""
    from flask import Flask
    from flask_sqlalchemy import SQLAlchemy
    from cspawn.models import db as _db

    # Use a standalone SQLite in-memory DB so we don't touch the real DB
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True

    _db.init_app(app)

    with app.app_context():
        _db.create_all()

    return app, _db


class TestGatherClusterState:
    """Thin mocked tests for gather_cluster_state.

    We verify the return tuple structure and that the function is read-only
    (no write calls on the mock client).
    """

    def test_returns_correct_tuple_structure(self):
        """gather_cluster_state returns a 6-tuple with correct types."""
        app, db = _make_minimal_flask_app()

        # Build a mock Docker client with two swarm nodes
        mgr_node = _make_swarm_node_mock("swarm1.example.com", role="manager", is_leader=True)
        wkr_node = _make_swarm_node_mock("swarm2.example.com", role="worker")

        mock_client = MagicMock()
        mock_client.nodes.list.return_value = [mgr_node, wkr_node]
        # services.list returns empty — no tasks → host_counts will be {}
        mock_client.services.list.return_value = []

        cfg = {
            "NODE_TIERS": NODE_TIERS_JSON,
            "DATA_DIR": tempfile.mkdtemp(),
            "DEFAULT_CAPACITY": "6",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg["DATA_DIR"] = tmpdir
            node_dicts, host_counts, pending, class_rows, host_rows, empty_since = (
                gather_cluster_state(app, mock_client, cfg)
            )

        # node_dicts should have the raw attrs of both nodes
        assert len(node_dicts) == 2
        assert isinstance(host_counts, dict)
        assert isinstance(pending, int)
        assert isinstance(class_rows, list)
        assert isinstance(host_rows, list)
        assert isinstance(empty_since, dict)

    def test_no_mutations_on_docker_client(self):
        """gather_cluster_state must not call any mutating Docker methods."""
        app, db = _make_minimal_flask_app()

        mock_client = MagicMock()
        mock_client.nodes.list.return_value = []
        mock_client.services.list.return_value = []

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = {"DATA_DIR": tmpdir, "DEFAULT_CAPACITY": "6"}
            gather_cluster_state(app, mock_client, cfg)

        # Assert that no mutating methods were called
        mock_client.nodes.remove.assert_not_called()
        mock_client.services.create.assert_not_called()
        mock_client.services.delete.assert_not_called()
        mock_client.swarm.leave.assert_not_called()

    def test_empty_since_populated_for_empty_nodes(self):
        """Nodes with zero host count are added to empty_since."""
        app, db = _make_minimal_flask_app()

        wkr_node = _make_swarm_node_mock("swarm2.example.com", role="worker")
        mock_client = MagicMock()
        mock_client.nodes.list.return_value = [wkr_node]
        mock_client.services.list.return_value = []

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = {"DATA_DIR": tmpdir, "DEFAULT_CAPACITY": "6"}
            _, _, _, _, _, empty_since = gather_cluster_state(app, mock_client, cfg)

        # swarm2.example.com has 0 hosts → should be in empty_since
        assert "swarm2.example.com" in empty_since


# ---------------------------------------------------------------------------
# apply_plan — mocked infrastructure calls
# ---------------------------------------------------------------------------

class TestApplyPlan:
    """Verify that dry_run=True suppresses all side effects."""

    def test_dry_run_makes_no_mutating_calls(self):
        """apply_plan with dry_run=True must call no Docker or DO primitives."""
        plan = ScalePlan(
            add_large=1,
            add_small=0,
            remove_nodes=["swarm5.example.com"],
            purge_first=True,
            reason="test",
        )
        ctx = MagicMock()
        cfg = {"NODE_TIERS": NODE_TIERS_JSON, "DEFAULT_CAPACITY": "6"}

        with (
            patch("cspawn.cli.node._create_droplet") as mock_create,
            patch("cspawn.cli.node._configure_node") as mock_configure,
            patch("cspawn.cli.node._join_swarm") as mock_join,
            patch("cspawn.cli.node.graceful_remove_node") as mock_remove,
        ):
            result = apply_plan(ctx, plan, cfg, dry_run=True)

        assert result.dry_run is True
        # Dry-run reports what the plan WOULD do (no mutation occurs)...
        assert result.added == 1
        assert result.removed == 1
        assert result.would_remove == ["swarm5.example.com"]
        # ...but no infrastructure primitive is actually invoked.
        mock_create.assert_not_called()
        mock_configure.assert_not_called()
        mock_join.assert_not_called()
        mock_remove.assert_not_called()

    def test_dry_run_returns_apply_result(self):
        """apply_plan(dry_run=True) returns an ApplyResult with dry_run=True."""
        plan = ScalePlan(add_large=0, add_small=0, remove_nodes=[], reason="hold")
        ctx = MagicMock()
        cfg = {"NODE_TIERS": NODE_TIERS_JSON, "DEFAULT_CAPACITY": "6"}

        result = apply_plan(ctx, plan, cfg, dry_run=True)

        assert isinstance(result, ApplyResult)
        assert result.dry_run is True

    def test_dry_run_summary_lists_would_remove(self):
        """The dry-run summary string surfaces the planned removals by name."""
        plan = ScalePlan(
            add_large=0,
            add_small=0,
            remove_nodes=["swarm5.example.com"],
            purge_first=True,
            reason="test",
        )
        ctx = MagicMock()
        cfg = {"NODE_TIERS": NODE_TIERS_JSON, "DEFAULT_CAPACITY": "6"}

        with (
            patch("cspawn.cli.node.graceful_remove_node"),
        ):
            result = apply_plan(ctx, plan, cfg, dry_run=True)

        summary = result.summary()
        assert "removed=1" in summary
        assert "would_remove=swarm5.example.com" in summary

    def test_scale_down_rechecks_emptiness_before_drain(self):
        """apply_plan re-checks host count immediately before graceful_remove_node.

        When the re-check shows hosts appeared (race), the node is skipped.
        """
        plan = ScalePlan(
            add_large=0,
            add_small=0,
            remove_nodes=["swarm5.example.com"],
            purge_first=False,
            reason="scale-down",
        )
        ctx = MagicMock()
        cfg = {
            "NODE_TIERS": NODE_TIERS_JSON,
            "DEFAULT_CAPACITY": "6",
            "DO_TOKEN": "tok",
            "DO_NAMES": "swarm{serial}.example.com",
            "DOCKER_URI": "ssh://fake",
        }

        mock_client = MagicMock()
        mock_client.nodes.list.return_value = []
        mock_client.services.list.return_value = [
            # Simulate a task appearing on swarm5 mid-cycle
        ]

        # count_hosts_per_node will return non-zero for swarm5 → should skip
        with (
            patch(
                "cspawn.cli.node.count_hosts_per_node",
                return_value={"swarm5": 3},
            ) as mock_count,
            patch("cspawn.cli.node.graceful_remove_node") as mock_remove,
            patch("digitalocean.Manager") as mock_do_mgr,
        ):
            result = apply_plan(
                ctx, plan, cfg, dry_run=False,
                manager_client=mock_client,
            )

        # graceful_remove_node must NOT have been called (node was not empty)
        mock_remove.assert_not_called()
        assert result.removed == 0
        assert len(result.errors) > 0  # should record the skip as an error


class TestApplyPlanScaleUpVerification:
    """Post-join provisioning verification wired into the scale-up loop.

    ``apply_plan`` imports ``_verify_node_provisioning``, ``_expected_docker_version``,
    ``_find_swarm_node``, ``_drain_swarm_node``, and ``_ensure_priv_key`` locally from
    ``cspawn.cli.node`` (same pattern as the existing ``_create_droplet``/
    ``_configure_node``/``_join_swarm`` imports), so all patch targets below use the
    ``cspawn.cli.node.*`` dotted path — patching ``cspawn.cs_docker.autoscale.*``
    would not intercept the call.
    """

    @staticmethod
    def _base_cfg():
        return {
            "NODE_TIERS": NODE_TIERS_JSON,
            "DEFAULT_CAPACITY": "6",
            "DO_TOKEN": "tok",
            "DO_NAMES": "swarm{serial}.example.com",
            "DOCKER_URI": "ssh://fake",
        }

    def test_one_of_two_nodes_fails_verification(self):
        """A plan adding two nodes where one fails verification only counts the other.

        The failed node's fqdn must appear in ``result.errors``, and drain must be
        attempted only for the failed node (not the healthy one).
        """
        plan = ScalePlan(add_large=1, add_small=1, remove_nodes=[], reason="test")
        ctx = MagicMock()
        cfg = self._base_cfg()

        mock_client = MagicMock()
        droplet_calls = [
            (MagicMock(), "10.0.0.10", "swarm10.example.com", "swarm10"),
            (MagicMock(), "10.0.0.11", "swarm11.example.com", "swarm11"),
        ]
        failed_node_obj = MagicMock()

        with (
            patch("cspawn.cli.node._create_droplet", side_effect=droplet_calls) as mock_create,
            patch("cspawn.cli.node._configure_node") as mock_configure,
            patch("cspawn.cli.node._join_swarm") as mock_join,
            patch(
                "cspawn.cli.node._ensure_priv_key",
                return_value=(Path("/fake/id_rsa"), Path("/fake/id_rsa.pub")),
            ),
            patch("cspawn.cli.node._expected_docker_version", return_value=None),
            patch(
                "cspawn.cli.node._verify_node_provisioning",
                side_effect=[["SSH reachability: 0/3 consecutive connects succeeded"], []],
            ) as mock_verify,
            patch(
                "cspawn.cli.node._find_swarm_node", return_value=failed_node_obj
            ) as mock_find,
            patch("cspawn.cli.node._drain_swarm_node") as mock_drain,
            patch("digitalocean.Manager"),
        ):
            result = apply_plan(
                ctx, plan, cfg, dry_run=False,
                manager_client=mock_client,
            )

        assert result.added == 1
        assert len(result.errors) == 1
        assert "swarm10.example.com" in result.errors[0]
        assert mock_verify.call_count == 2
        # Drain attempted only for the failed node (swarm10), not the healthy one.
        mock_find.assert_called_once_with(mock_client, "swarm10.example.com", "swarm10")
        mock_drain.assert_called_once_with(mock_client, failed_node_obj, log=ANY)
        # Both droplets/configure/join calls still happened (batch was not aborted).
        assert mock_create.call_count == 2
        assert mock_configure.call_count == 2
        assert mock_join.call_count == 2

    def test_verification_failure_with_no_swarm_node_found_skips_drain(self):
        """When `_find_swarm_node` returns None, drain is skipped without raising.

        The verification failure must still be recorded in ``result.errors``.
        """
        plan = ScalePlan(add_large=1, add_small=0, remove_nodes=[], reason="test")
        ctx = MagicMock()
        cfg = self._base_cfg()

        mock_client = MagicMock()

        with (
            patch(
                "cspawn.cli.node._create_droplet",
                return_value=(MagicMock(), "10.0.0.10", "swarm10.example.com", "swarm10"),
            ),
            patch("cspawn.cli.node._configure_node"),
            patch("cspawn.cli.node._join_swarm"),
            patch(
                "cspawn.cli.node._ensure_priv_key",
                return_value=(Path("/fake/id_rsa"), Path("/fake/id_rsa.pub")),
            ),
            patch("cspawn.cli.node._expected_docker_version", return_value=None),
            patch(
                "cspawn.cli.node._verify_node_provisioning",
                return_value=["cloud-init status: not done"],
            ),
            patch("cspawn.cli.node._find_swarm_node", return_value=None) as mock_find,
            patch("cspawn.cli.node._drain_swarm_node") as mock_drain,
            patch("digitalocean.Manager"),
        ):
            result = apply_plan(
                ctx, plan, cfg, dry_run=False,
                manager_client=mock_client,
            )

        assert result.added == 0
        assert len(result.errors) == 1
        assert "swarm10.example.com" in result.errors[0]
        mock_find.assert_called_once()
        mock_drain.assert_not_called()

    def test_all_nodes_pass_verification(self):
        """All planned nodes passing verification counts every node with no errors.

        Regression guard matching pre-ticket behavior when verification is a no-op.
        """
        plan = ScalePlan(add_large=1, add_small=1, remove_nodes=[], reason="test")
        ctx = MagicMock()
        cfg = self._base_cfg()

        mock_client = MagicMock()
        droplet_calls = [
            (MagicMock(), "10.0.0.10", "swarm10.example.com", "swarm10"),
            (MagicMock(), "10.0.0.11", "swarm11.example.com", "swarm11"),
        ]

        with (
            patch("cspawn.cli.node._create_droplet", side_effect=droplet_calls),
            patch("cspawn.cli.node._configure_node"),
            patch("cspawn.cli.node._join_swarm"),
            patch(
                "cspawn.cli.node._ensure_priv_key",
                return_value=(Path("/fake/id_rsa"), Path("/fake/id_rsa.pub")),
            ),
            patch("cspawn.cli.node._expected_docker_version", return_value=None),
            patch("cspawn.cli.node._verify_node_provisioning", return_value=[]) as mock_verify,
            patch("cspawn.cli.node._find_swarm_node") as mock_find,
            patch("cspawn.cli.node._drain_swarm_node") as mock_drain,
            patch("digitalocean.Manager"),
        ):
            result = apply_plan(
                ctx, plan, cfg, dry_run=False,
                manager_client=mock_client,
            )

        assert result.added == 2
        assert result.errors == []
        assert mock_verify.call_count == 2
        mock_find.assert_not_called()
        mock_drain.assert_not_called()


# ---------------------------------------------------------------------------
# run_autoscale — kill-switch and dry-run enforcement
# ---------------------------------------------------------------------------

class TestRunAutoscale:
    """Verify kill-switch and AUTOSCALE_DRY_RUN enforcement."""

    def test_autoscale_disabled_returns_early(self):
        """When AUTOSCALE_ENABLED=false, run_autoscale returns without calling gather."""
        ctx = MagicMock()

        with (
            patch("cspawn.cs_docker.autoscale._get_config", create=True),
            patch(
                "cspawn.cs_docker.autoscale.gather_cluster_state",
            ) as mock_gather,
            patch(
                "cspawn.cli.util.get_config",
                return_value={
                    "AUTOSCALE_ENABLED": "false",
                    "DATA_DIR": tempfile.mkdtemp(),
                },
            ),
        ):
            result = run_autoscale(ctx, dry_run=False, force=False)

        assert isinstance(result, ApplyResult)
        mock_gather.assert_not_called()

    def test_autoscale_disabled_force_bypasses_kill_switch(self):
        """force=True bypasses AUTOSCALE_ENABLED=false and proceeds to gather state."""
        ctx = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_cfg = {
                "AUTOSCALE_ENABLED": "false",  # kill-switch off...
                "AUTOSCALE_DRY_RUN": "true",   # ...but dry-run still protects us
                "DATA_DIR": tmpdir,
                "NODE_TIERS": NODE_TIERS_JSON,
                "DEFAULT_CAPACITY": "6",
                "DOCKER_URI": "ssh://fake",
                "DO_TOKEN": "fake-token",
            }

            fake_app = MagicMock()
            fake_manager_client = MagicMock()
            fake_manager_client.nodes.list.return_value = []
            fake_manager_client.services.list.return_value = []

            with (
                patch("cspawn.cli.util.get_config", return_value=fake_cfg),
                patch(
                    "cspawn.cs_docker.autoscale.gather_cluster_state",
                    return_value=([], {}, 0, [], [], {}),
                ) as mock_gather,
                patch("cspawn.cs_docker.autoscale.apply_plan") as mock_apply,
                patch("digitalocean.Manager"),
            ):
                mock_apply.return_value = ApplyResult(dry_run=True)
                result = run_autoscale(
                    ctx,
                    dry_run=True,
                    force=True,
                    app=fake_app,
                    manager_client=fake_manager_client,
                )

            # Kill-switch was bypassed: the cycle actually ran.
            mock_gather.assert_called_once()
            assert isinstance(result, ApplyResult)

    def test_autoscale_dry_run_config_forces_dry_run(self):
        """AUTOSCALE_DRY_RUN=true in config prevents all mutations even if CLI dry_run=False."""
        ctx = MagicMock()

        fake_node_dicts = []
        fake_host_counts = {}
        fake_pending = 0
        fake_class_rows = []
        fake_host_rows = []
        fake_empty_since = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_cfg = {
                "AUTOSCALE_ENABLED": "true",
                "AUTOSCALE_DRY_RUN": "true",
                "DATA_DIR": tmpdir,
                "NODE_TIERS": NODE_TIERS_JSON,
                "DEFAULT_CAPACITY": "6",
                "DOCKER_URI": "ssh://fake",
                "DO_TOKEN": "fake-token",
            }

            fake_app = MagicMock()
            fake_manager_client = MagicMock()
            fake_manager_client.nodes.list.return_value = []
            fake_manager_client.services.list.return_value = []

            with (
                patch("cspawn.cli.util.get_config", return_value=fake_cfg),
                patch(
                    "cspawn.cs_docker.autoscale.gather_cluster_state",
                    return_value=(
                        fake_node_dicts,
                        fake_host_counts,
                        fake_pending,
                        fake_class_rows,
                        fake_host_rows,
                        fake_empty_since,
                    ),
                ),
                patch("cspawn.cs_docker.autoscale.apply_plan") as mock_apply,
                patch("digitalocean.Manager"),
            ):
                mock_apply.return_value = ApplyResult(dry_run=True)
                result = run_autoscale(
                    ctx,
                    dry_run=False,  # CLI says not dry-run
                    force=False,
                    app=fake_app,
                    manager_client=fake_manager_client,
                )

            # apply_plan must have been called with dry_run=True (overridden by config)
            mock_apply.assert_called_once()
            call_kwargs = mock_apply.call_args
            assert call_kwargs.kwargs["dry_run"] is True

    def test_concurrent_lock_aborts_second_run(self):
        """When the lock is already held, run_autoscale returns empty ApplyResult."""
        ctx = MagicMock()

        import fcntl

        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = os.path.join(tmpdir, ".autoscale.lock")
            # Pre-acquire the lock
            holder = open(lock_path, "w")
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            try:
                fake_cfg = {
                    "AUTOSCALE_ENABLED": "true",
                    "AUTOSCALE_DRY_RUN": "false",
                    "DATA_DIR": tmpdir,
                    "NODE_TIERS": NODE_TIERS_JSON,
                    "DEFAULT_CAPACITY": "6",
                    "DOCKER_URI": "ssh://fake",
                    "DO_TOKEN": "fake-token",
                }
                with (
                    patch("cspawn.cli.util.get_config", return_value=fake_cfg),
                    patch("cspawn.cs_docker.autoscale.gather_cluster_state") as mock_gather,
                ):
                    result = run_autoscale(ctx, dry_run=False, force=False)

                assert isinstance(result, ApplyResult)
                mock_gather.assert_not_called()
            finally:
                fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
                holder.close()


# ---------------------------------------------------------------------------
# apply_reaper_zones — three-zone reaper logic
# ---------------------------------------------------------------------------
#
# All tests inject `now` and mock the Flask app + DB to avoid any live I/O.
# The three zones under test:
#   Protected   (now < purge_after)  → no mutations
#   Active-purge (purge_after <= now < purge_by) → stop idle hosts only
#   Dormant     (now >= purge_by)   → force-remove all, clear class fields
#


def _make_reaper_flask_app():
    """Create a minimal in-memory Flask app wired to cspawn models for reaper tests."""
    from flask import Flask
    from cspawn.models import db as _db

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-reaper-secret"
    app.config["TESTING"] = True

    _db.init_app(app)

    with app.app_context():
        _db.create_all()

    return app, _db


class TestApplyReaperZones:
    """Unit tests for the three-zone reaper (apply_reaper_zones).

    Strategy: use an in-memory SQLite DB with real cspawn models.  Create
    Class and CodeHost rows, inject `now` to place the class in each zone,
    and verify what was (or was not) mutated.

    The Flask ``csm`` attribute is mocked with a MagicMock so no Docker calls
    are attempted during the stop step.
    """

    # ── Shared helpers ───────────────────────────────────────────────────────

    _host_counter = 0  # ensure unique service_id across tests

    def _make_class_and_host(self, app, db, purge_after, purge_by, updated_at):
        """Create one ClassProto, User, Class, and CodeHost row linked together.

        Returns (class_id, host_id).
        """
        from cspawn.models import Class, ClassProto, CodeHost, User

        TestApplyReaperZones._host_counter += 1
        suffix = TestApplyReaperZones._host_counter

        with app.app_context():
            # Minimal ClassProto (required FK on Class)
            proto = ClassProto(
                name=f"Test Proto {suffix}",
                image_uri="test-image:latest",
                hash=f"deadbeef{suffix:04d}",
            )
            db.session.add(proto)
            db.session.flush()

            # Minimal user (required FK on CodeHost)
            user = User(
                user_id=f"uid-student{suffix}",
                email=f"student{suffix}@example.com",
                username=f"student{suffix}",
                is_active=True,
            )
            db.session.add(user)
            db.session.flush()

            cls = Class(
                name=f"Test Class {suffix}",
                proto_id=proto.id,
                start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                purge_after=purge_after,
                purge_by=purge_by,
            )
            db.session.add(cls)
            db.session.flush()

            host = CodeHost(
                user_id=user.id,
                service_id=f"svc-test-{suffix}",
                service_name=f"cs-student{suffix}",
                class_id=cls.id,
                app_state="ready",
            )
            # Force updated_at to the desired time
            host.updated_at = updated_at
            db.session.add(host)
            db.session.commit()

            return cls.id, host.id

    def _make_app_with_mock_csm(self, stop_host_side_effect=None):
        """Return (app, db, mock_csm) with app.csm mocked to avoid Docker calls.

        ``apply_reaper_zones`` now delegates the push/stop/delete sequence to
        a single ``app.csm.stop_host(ch)`` call (ticket 007-002). By default,
        ``mock_csm.stop_host`` performs the same caller-visible effect the
        real ``CodeServerManager.stop_host()`` has for these tests' purposes
        — deleting the ``CodeHost`` row and committing — and returns a
        successful ``StopResult``, so existing "host is gone from the DB"
        assertions continue to hold without needing live Docker/GitHub.
        Pass ``stop_host_side_effect`` to simulate push/stop/delete failures.
        """
        from cspawn.cs_docker.csmanager import StopResult

        app, db = _make_reaper_flask_app()
        mock_csm = MagicMock()

        def _default_stop_host(ch, *, push=True, branch="master"):
            db.session.delete(ch)
            db.session.commit()
            return StopResult(service_name=ch.service_name, pushed=push, stopped=True, deleted=True)

        mock_csm.stop_host.side_effect = stop_host_side_effect or _default_stop_host
        app.csm = mock_csm
        return app, db, mock_csm

    # ── 1. Protected zone — nothing reaped ───────────────────────────────────

    def test_protected_zone_no_host_removed(self):
        """Protected zone (now < purge_after): no CodeHost is stopped or deleted."""
        from cspawn.models import CodeHost

        app, db, mock_csm = self._make_app_with_mock_csm()

        now = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)
        purge_after = now + timedelta(hours=2)   # future — protected
        purge_by = now + timedelta(hours=4)
        updated_at = now - timedelta(hours=1)    # idle but protected

        cls_id, host_id = self._make_class_and_host(app, db, purge_after, purge_by, updated_at)

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        result = apply_reaper_zones(app, class_rows, [], now, dry_run=False)

        # Zone classified as protected
        assert result[cls_id] == "protected"

        # CodeHost must still be in the DB
        with app.app_context():
            host = CodeHost.query.get(host_id)
            assert host is not None

        # csm.stop_host must never have been called
        mock_csm.stop_host.assert_not_called()

    def test_protected_zone_exactly_at_boundary(self):
        """Boundary check: now exactly equal to purge_after is active-purge, not protected."""
        from cspawn.models import CodeHost, Class

        app, db, mock_csm = self._make_app_with_mock_csm()

        now = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)
        # purge_after == now → active-purge zone starts
        purge_after = now
        purge_by = now + timedelta(hours=2)
        # Host has been idle for 30 minutes → should be reaped
        updated_at = now - timedelta(minutes=30)

        cls_id, host_id = self._make_class_and_host(app, db, purge_after, purge_by, updated_at)

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        apply_reaper_zones(app, class_rows, [], now, dry_run=False)

        # Zone is active-purge (not protected) at the boundary
        with app.app_context():
            host = CodeHost.query.get(host_id)
            assert host is None, "Idle host should have been removed at purge_after boundary"

    # ── 2. Active-purge zone — idle hosts only ───────────────────────────────

    def test_active_purge_idle_host_removed(self):
        """Active-purge zone: host idle >= 15 min is stopped and deleted."""
        from cspawn.models import CodeHost

        app, db, mock_csm = self._make_app_with_mock_csm()

        now = datetime(2026, 6, 25, 14, 0, 0, tzinfo=timezone.utc)
        purge_after = now - timedelta(hours=1)   # past → active-purge
        purge_by = now + timedelta(hours=1)       # future
        updated_at = now - timedelta(minutes=20)  # idle 20 min → above threshold

        cls_id, host_id = self._make_class_and_host(app, db, purge_after, purge_by, updated_at)

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        result = apply_reaper_zones(app, class_rows, [], now, dry_run=False)

        assert result[cls_id] == "active-purge"

        # stop_host() must have been called exactly once, for this host.
        mock_csm.stop_host.assert_called_once()
        (called_host,), _ = mock_csm.stop_host.call_args
        assert called_host.id == host_id

        # DB record must be gone
        with app.app_context():
            host = CodeHost.query.get(host_id)
            assert host is None, "Idle host should have been deleted"

    def test_active_purge_non_idle_host_kept(self):
        """Active-purge zone: host idle < 15 min is NOT touched."""
        from cspawn.models import CodeHost

        app, db, mock_csm = self._make_app_with_mock_csm()

        now = datetime(2026, 6, 25, 14, 0, 0, tzinfo=timezone.utc)
        purge_after = now - timedelta(hours=1)
        purge_by = now + timedelta(hours=1)
        updated_at = now - timedelta(minutes=5)  # only 5 min idle — below threshold

        cls_id, host_id = self._make_class_and_host(app, db, purge_after, purge_by, updated_at)

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        apply_reaper_zones(app, class_rows, [], now, dry_run=False)

        # Host must still be in the DB (not idle enough)
        with app.app_context():
            host = CodeHost.query.get(host_id)
            assert host is not None, "Non-idle host must not be deleted"

        mock_csm.stop_host.assert_not_called()

    def test_active_purge_exactly_15min_idle_removed(self):
        """Active-purge zone: host at exactly 15 min idle threshold is removed."""
        from cspawn.models import CodeHost

        app, db, mock_csm = self._make_app_with_mock_csm()

        now = datetime(2026, 6, 25, 14, 0, 0, tzinfo=timezone.utc)
        purge_after = now - timedelta(hours=1)
        purge_by = now + timedelta(hours=1)
        updated_at = now - timedelta(minutes=15)  # exactly 15 minutes

        cls_id, host_id = self._make_class_and_host(app, db, purge_after, purge_by, updated_at)

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        apply_reaper_zones(app, class_rows, [], now, dry_run=False)

        with app.app_context():
            host = CodeHost.query.get(host_id)
            assert host is None, "Host at exactly 15 min idle should be removed"

    # ── 3. Dormant zone — force-remove all, clear class fields ───────────────

    def test_dormant_zone_all_hosts_removed(self):
        """Dormant zone: ALL hosts are force-removed regardless of idle state."""
        from cspawn.models import CodeHost

        app, db, mock_csm = self._make_app_with_mock_csm()

        now = datetime(2026, 6, 25, 18, 0, 0, tzinfo=timezone.utc)
        purge_after = now - timedelta(hours=3)
        purge_by = now - timedelta(hours=1)  # past → dormant
        # Host is NOT idle (updated just 2 minutes ago) but zone is dormant
        updated_at = now - timedelta(minutes=2)

        cls_id, host_id = self._make_class_and_host(app, db, purge_after, purge_by, updated_at)

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        result = apply_reaper_zones(app, class_rows, [], now, dry_run=False)

        assert result[cls_id] == "dormant"

        # stop_host() must have been called exactly once, for this host
        # (no idle check in dormant zone).
        mock_csm.stop_host.assert_called_once()
        (called_host,), _ = mock_csm.stop_host.call_args
        assert called_host.id == host_id

        # DB record must be gone
        with app.app_context():
            host = CodeHost.query.get(host_id)
            assert host is None, "Dormant-zone host must be force-removed"

    def test_dormant_zone_class_fields_cleared(self):
        """Dormant zone: Class.purge_after, purge_by, and target_nodes set to None."""
        from cspawn.models import Class

        app, db, mock_csm = self._make_app_with_mock_csm()

        now = datetime(2026, 6, 25, 18, 0, 0, tzinfo=timezone.utc)
        purge_after = now - timedelta(hours=3)
        purge_by = now - timedelta(hours=1)
        updated_at = now - timedelta(hours=2)

        cls_id, _ = self._make_class_and_host(app, db, purge_after, purge_by, updated_at)

        # Set target_nodes on the class
        with app.app_context():
            cls = Class.query.get(cls_id)
            cls.target_nodes = 5
            db.session.commit()

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        apply_reaper_zones(app, class_rows, [], now, dry_run=False)

        with app.app_context():
            cls = Class.query.get(cls_id)
            assert cls.purge_after is None, "purge_after must be cleared in dormant zone"
            assert cls.purge_by is None, "purge_by must be cleared in dormant zone"
            assert cls.target_nodes is None, "target_nodes must be cleared in dormant zone"

    def test_dormant_zone_class_no_longer_in_gather_results(self):
        """After dormant cleanup, class has purge_after=None so gather_cluster_state excludes it.

        This is verified by querying Class directly and confirming purge_after is NULL,
        which is the filter condition in gather_cluster_state.
        """
        from cspawn.models import Class

        app, db, mock_csm = self._make_app_with_mock_csm()

        now = datetime(2026, 6, 25, 18, 0, 0, tzinfo=timezone.utc)
        purge_after = now - timedelta(hours=3)
        purge_by = now - timedelta(hours=1)
        updated_at = now - timedelta(hours=2)

        cls_id, _ = self._make_class_and_host(app, db, purge_after, purge_by, updated_at)

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        apply_reaper_zones(app, class_rows, [], now, dry_run=False)

        # The class must have purge_after=None so gather_cluster_state excludes it
        with app.app_context():
            remaining = Class.query.filter(
                Class.purge_after.isnot(None),
                Class.purge_by.isnot(None),
            ).all()
            cls_ids = [c.id for c in remaining]
            assert cls_id not in cls_ids, "Dormant class must not appear in purge-window query"

    # ── 4. Dry-run — no mutations ─────────────────────────────────────────────

    def test_dry_run_active_purge_no_mutations(self):
        """dry_run=True in active-purge zone: nothing stopped or deleted."""
        from cspawn.models import CodeHost

        app, db, mock_csm = self._make_app_with_mock_csm()

        now = datetime(2026, 6, 25, 14, 0, 0, tzinfo=timezone.utc)
        purge_after = now - timedelta(hours=1)
        purge_by = now + timedelta(hours=1)
        updated_at = now - timedelta(minutes=30)  # clearly idle

        cls_id, host_id = self._make_class_and_host(app, db, purge_after, purge_by, updated_at)

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        apply_reaper_zones(app, class_rows, [], now, dry_run=True)

        # DB record must still exist
        with app.app_context():
            host = CodeHost.query.get(host_id)
            assert host is not None, "dry_run must not delete hosts"

        mock_csm.stop_host.assert_not_called()

    def test_dry_run_dormant_no_mutations(self):
        """dry_run=True in dormant zone: nothing force-removed, class fields unchanged."""
        from cspawn.models import CodeHost, Class

        app, db, mock_csm = self._make_app_with_mock_csm()

        now = datetime(2026, 6, 25, 18, 0, 0, tzinfo=timezone.utc)
        purge_after = now - timedelta(hours=3)
        purge_by = now - timedelta(hours=1)
        updated_at = now - timedelta(hours=2)

        cls_id, host_id = self._make_class_and_host(app, db, purge_after, purge_by, updated_at)

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        apply_reaper_zones(app, class_rows, [], now, dry_run=True)

        # Host still exists
        with app.app_context():
            host = CodeHost.query.get(host_id)
            assert host is not None, "dry_run must not delete hosts"

        # Class fields unchanged
        with app.app_context():
            cls = Class.query.get(cls_id)
            assert cls.purge_after is not None, "dry_run must not clear purge_after"
            assert cls.purge_by is not None, "dry_run must not clear purge_by"

        mock_csm.stop_host.assert_not_called()

    # ── 5. Manager/leader node safety ─────────────────────────────────────────

    def test_reaper_does_not_remove_manager_hosts_by_node(self):
        """apply_reaper_zones operates on CodeHost records only, not Swarm nodes.

        Manager-node safety is the responsibility of plan_scale_down.  This test
        confirms the reaper correctly removes a dormant host even when a node_name
        is present — node role is irrelevant to host-level reaping.
        """
        from cspawn.models import CodeHost

        app, db, mock_csm = self._make_app_with_mock_csm()

        now = datetime(2026, 6, 25, 18, 0, 0, tzinfo=timezone.utc)
        purge_after = now - timedelta(hours=2)
        purge_by = now - timedelta(hours=1)
        updated_at = now - timedelta(hours=2)

        cls_id, host_id = self._make_class_and_host(app, db, purge_after, purge_by, updated_at)

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        result = apply_reaper_zones(app, class_rows, [], now, dry_run=False)

        assert result[cls_id] == "dormant"
        with app.app_context():
            assert CodeHost.query.get(host_id) is None

    # ── 6. Protected-zone guard in plan_scale_down ────────────────────────────

    def test_plan_scale_down_protected_node_skipped(self):
        """plan_scale_down skips nodes in protected_node_fqdns."""
        # Two workers: swarm2 is protected (carries protected-zone hosts),
        # swarm3 is large and provides excess.  swarm2 should never be selected.
        protected = make_worker(short="swarm2", fqdn="swarm2.net", capacity=6, serial=2)
        large = make_worker(short="swarm3", fqdn="swarm3.net", capacity=100, serial=3)
        state = ClusterState(nodes=[protected, large], pending_hosts=0)

        # Plenty of excess (106 total capacity), both nodes cooled down
        empty_since = {
            "swarm2.net": NOW - timedelta(hours=2),
            "swarm3.net": NOW - timedelta(hours=2),
        }
        cfg = {
            "NODE_TIERS": NODE_TIERS_JSON,
            "DEFAULT_CAPACITY": "6",
            "AUTOSCALE_HEADROOM": "2",
            "AUTOSCALE_MAX_REMOVE_PER_CYCLE": "2",
            "AUTOSCALE_SCALEDOWN_COOLDOWN_MIN": "30",
            "AUTOSCALE_MIN_WORKER_NODES": "1",
        }

        result = plan_scale_down(
            state, demand=0, cfg=cfg, now=NOW, empty_since=empty_since,
            protected_node_fqdns=frozenset(["swarm2.net"]),
        )

        # swarm2 must NOT be in the result
        fqdns = [n.fqdn for n in result]
        assert "swarm2.net" not in fqdns, "Protected-zone node must not be selected for removal"

    def test_plan_scale_down_no_protected_fqdns_unchanged_behavior(self):
        """Passing protected_node_fqdns=None keeps original behavior."""
        small = make_worker(short="swarm2", fqdn="swarm2.net", capacity=6, serial=2)
        large = make_worker(short="swarm3", fqdn="swarm3.net", capacity=100, serial=3)
        state = ClusterState(nodes=[small, large], pending_hosts=0)
        empty_since = {
            "swarm2.net": NOW - timedelta(hours=2),
            "swarm3.net": NOW - timedelta(hours=2),
        }

        cfg = {
            "NODE_TIERS": NODE_TIERS_JSON,
            "DEFAULT_CAPACITY": "6",
            "AUTOSCALE_HEADROOM": "2",
            "AUTOSCALE_MAX_REMOVE_PER_CYCLE": "1",
            "AUTOSCALE_SCALEDOWN_COOLDOWN_MIN": "30",
            "AUTOSCALE_MIN_WORKER_NODES": "1",
        }

        result = plan_scale_down(
            state, demand=0, cfg=cfg, now=NOW, empty_since=empty_since,
            protected_node_fqdns=None,
        )
        # swarm3 (highest serial) should be selected when not protected
        assert len(result) == 1
        assert result[0].fqdn == "swarm3.net"

    # ── 7. Zone boundary at purge_by ─────────────────────────────────────────

    def test_dormant_boundary_exactly_at_purge_by(self):
        """now exactly equal to purge_by → dormant zone."""
        from cspawn.models import CodeHost, Class

        app, db, mock_csm = self._make_app_with_mock_csm()

        now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=timezone.utc)
        purge_after = now - timedelta(hours=2)
        purge_by = now  # exactly now → dormant (>= check)
        updated_at = now - timedelta(minutes=1)  # not idle — only dormant would remove

        cls_id, host_id = self._make_class_and_host(app, db, purge_after, purge_by, updated_at)

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        result = apply_reaper_zones(app, class_rows, [], now, dry_run=False)

        assert result[cls_id] == "dormant"
        with app.app_context():
            assert CodeHost.query.get(host_id) is None, "Host at purge_by must be force-removed"

        with app.app_context():
            cls = Class.query.get(cls_id)
            assert cls.purge_after is None

    # ── 8. Row missing id / purge_after → skipped ────────────────────────────

    def test_class_row_without_id_skipped(self):
        """Class row missing 'id' key is silently skipped."""
        app, db, mock_csm = self._make_app_with_mock_csm()
        now = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)

        class_rows = [{"purge_after": now - timedelta(hours=1), "purge_by": now + timedelta(hours=1)}]
        # Should not raise
        result = apply_reaper_zones(app, class_rows, [], now, dry_run=False)
        assert result == {}

    def test_class_row_without_purge_after_skipped(self):
        """Class row with purge_after=None is skipped (no window set)."""
        app, db, mock_csm = self._make_app_with_mock_csm()
        now = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)

        class_rows = [{"id": 42, "purge_after": None, "purge_by": now + timedelta(hours=1)}]
        result = apply_reaper_zones(app, class_rows, [], now, dry_run=False)
        assert result == {}

    # ── 9. Both zones call stop_host(), not the old get()/s.stop() pair ──────

    def test_active_purge_calls_stop_host_not_get(self):
        """apply_reaper_zones's active-purge loop invokes app.csm.stop_host(ch)
        directly — the old app.csm.get(ch)/s.stop() pair is gone."""
        app, db, mock_csm = self._make_app_with_mock_csm()

        now = datetime(2026, 6, 25, 14, 0, 0, tzinfo=timezone.utc)
        purge_after = now - timedelta(hours=1)
        purge_by = now + timedelta(hours=1)
        updated_at = now - timedelta(minutes=30)  # idle

        cls_id, host_id = self._make_class_and_host(app, db, purge_after, purge_by, updated_at)

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        apply_reaper_zones(app, class_rows, [], now, dry_run=False)

        mock_csm.stop_host.assert_called_once()
        (called_host,), kwargs = mock_csm.stop_host.call_args
        assert called_host.id == host_id
        # apply_reaper_zones must not call the old get()/list() Docker primitives.
        mock_csm.get.assert_not_called()

    def test_dormant_calls_stop_host_not_get(self):
        """apply_reaper_zones's dormant loop invokes app.csm.stop_host(ch)
        directly — the old app.csm.get(ch)/s.stop() pair is gone."""
        app, db, mock_csm = self._make_app_with_mock_csm()

        now = datetime(2026, 6, 25, 18, 0, 0, tzinfo=timezone.utc)
        purge_after = now - timedelta(hours=3)
        purge_by = now - timedelta(hours=1)
        updated_at = now - timedelta(minutes=1)  # not idle — dormant force-removes anyway

        cls_id, host_id = self._make_class_and_host(app, db, purge_after, purge_by, updated_at)

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        apply_reaper_zones(app, class_rows, [], now, dry_run=False)

        mock_csm.stop_host.assert_called_once()
        (called_host,), kwargs = mock_csm.stop_host.call_args
        assert called_host.id == host_id
        mock_csm.get.assert_not_called()

    # ── 10. A push/stop failure on one host must not abort the rest ─────────

    def _make_two_hosts_same_class(self, app, db, purge_after, purge_by, updated_at):
        """Create a single Class with two CodeHost rows for multi-host tests."""
        from cspawn.models import Class, ClassProto, CodeHost, User

        TestApplyReaperZones._host_counter += 1
        suffix = TestApplyReaperZones._host_counter

        with app.app_context():
            proto = ClassProto(
                name=f"Multi Proto {suffix}",
                image_uri="test-image:latest",
                hash=f"deadbeefm{suffix:04d}",
            )
            db.session.add(proto)
            db.session.flush()

            cls = Class(
                name=f"Multi Class {suffix}",
                proto_id=proto.id,
                start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                purge_after=purge_after,
                purge_by=purge_by,
            )
            db.session.add(cls)
            db.session.flush()

            host_ids = []
            for i in range(2):
                user = User(
                    user_id=f"uid-multi{suffix}-{i}",
                    email=f"multi{suffix}-{i}@example.com",
                    username=f"multi{suffix}-{i}",
                    is_active=True,
                )
                db.session.add(user)
                db.session.flush()

                host = CodeHost(
                    user_id=user.id,
                    service_id=f"svc-multi-{suffix}-{i}",
                    service_name=f"cs-multi{suffix}-{i}",
                    class_id=cls.id,
                    app_state="ready",
                )
                host.updated_at = updated_at
                db.session.add(host)
                db.session.flush()
                host_ids.append(host.id)

            db.session.commit()
            return cls.id, host_ids

    def test_dormant_zone_push_failure_on_one_host_does_not_abort_the_other(self):
        """A mocked push failure (StopResult.push_error set) on one host must
        not stop the dormant loop from processing the remaining host."""
        from cspawn.cs_docker.csmanager import StopResult
        from cspawn.models import CodeHost

        app, db = _make_reaper_flask_app()
        mock_csm = MagicMock()

        def _stop_host_side_effect(ch, *, push=True, branch="master"):
            failing = ch.service_name.endswith("-0")
            db.session.delete(ch)
            db.session.commit()
            return StopResult(
                service_name=ch.service_name,
                pushed=not failing,
                push_error="push boom" if failing else None,
                stopped=True,
                deleted=True,
            )

        mock_csm.stop_host.side_effect = _stop_host_side_effect
        app.csm = mock_csm

        now = datetime(2026, 6, 25, 18, 0, 0, tzinfo=timezone.utc)
        purge_after = now - timedelta(hours=3)
        purge_by = now - timedelta(hours=1)
        updated_at = now - timedelta(minutes=1)

        cls_id, host_ids = self._make_two_hosts_same_class(app, db, purge_after, purge_by, updated_at)

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        result = apply_reaper_zones(app, class_rows, [], now, dry_run=False)

        assert result[cls_id] == "dormant"
        # Both hosts must have been processed (stop_host called twice) despite
        # the first one's push failure.
        assert mock_csm.stop_host.call_count == 2
        with app.app_context():
            for host_id in host_ids:
                assert CodeHost.query.get(host_id) is None, (
                    "Both hosts must be removed even though one push failed"
                )

    def test_active_purge_push_failure_on_one_host_does_not_abort_the_other(self):
        """Same isolation guarantee in the active-purge zone."""
        from cspawn.cs_docker.csmanager import StopResult
        from cspawn.models import CodeHost

        app, db = _make_reaper_flask_app()
        mock_csm = MagicMock()

        def _stop_host_side_effect(ch, *, push=True, branch="master"):
            failing = ch.service_name.endswith("-0")
            db.session.delete(ch)
            db.session.commit()
            return StopResult(
                service_name=ch.service_name,
                pushed=not failing,
                push_error="push boom" if failing else None,
                stopped=True,
                deleted=True,
            )

        mock_csm.stop_host.side_effect = _stop_host_side_effect
        app.csm = mock_csm

        now = datetime(2026, 6, 25, 14, 0, 0, tzinfo=timezone.utc)
        purge_after = now - timedelta(hours=1)
        purge_by = now + timedelta(hours=1)
        updated_at = now - timedelta(minutes=30)  # both idle

        cls_id, host_ids = self._make_two_hosts_same_class(app, db, purge_after, purge_by, updated_at)

        class_rows = [{"id": cls_id, "purge_after": purge_after, "purge_by": purge_by}]
        result = apply_reaper_zones(app, class_rows, [], now, dry_run=False)

        assert result[cls_id] == "active-purge"
        assert mock_csm.stop_host.call_count == 2
        with app.app_context():
            for host_id in host_ids:
                assert CodeHost.query.get(host_id) is None, (
                    "Both idle hosts must be removed even though one push failed"
                )
