"""
Unit tests for the `node rebalance` planner (plan_rebalance) and, since
sprint-014 ticket-003, for `rebalance()`'s interaction with
`_pin_service_to_node()` when relocating a create-time-pinned host.

Pure-function tests for the move-planning logic — no Docker, DB, or network.
The greedy planner should level per-node host counts to a spread of at most
one, respect the eligible-node set, fill brand-new empty nodes, drain
non-eligible nodes, and honor max_moves.

The `TestRebalanceRepinsCreateTimePinnedHost` section below covers sprint-014
ticket-003: once every codehost is pinned at creation (Ticket 001), the fleet
`rebalance()` operates on is fully pinned by default, and `rebalance()` must
continue to relocate hosts against that fleet exactly as it always has --
`plan_rebalance()` never reads constraints (only live per-node placement), and
`_pin_service_to_node()` replaces rather than accumulates any prior
`node.hostname==` pin, whether that pin came from an earlier rebalance or from
Ticket 001's create-time path. No production code is touched by these tests;
see the ticket for the "if this reveals a real gap, throw an exception rather
than patch around it" rule.

Run with::

    uv run pytest test/test_node_rebalance.py -v
"""
from __future__ import annotations

from collections import Counter
from unittest.mock import MagicMock

from cspawn.cli.node import _pin_service_to_node, _service_constraints, plan_rebalance


def _spread_after(per_node, eligible, moves):
    """Apply moves and return (max-min) count across eligible nodes."""
    counts = Counter()
    for n in eligible:
        counts[n] = 0
    for n, users in per_node.items():
        counts[n] += len(users)
    for user, src, tgt in moves:
        counts[src] -= 1
        counts[tgt] += 1
    eligible_counts = [counts[n] for n in eligible]
    return max(eligible_counts) - min(eligible_counts)


def test_already_balanced_no_moves():
    per_node = {"swarm1": ["a", "b"], "swarm2": ["c", "d"]}
    eligible = ["swarm1", "swarm2"]
    assert plan_rebalance(per_node, eligible) == []


def test_off_by_one_is_left_alone():
    # 3 vs 2 cannot be improved by an integer move; spread is already minimal.
    per_node = {"swarm1": ["a", "b", "c"], "swarm2": ["d", "e"]}
    eligible = ["swarm1", "swarm2"]
    assert plan_rebalance(per_node, eligible) == []


def test_simple_two_node_levels_out():
    per_node = {"swarm1": ["a", "b", "c", "d"], "swarm2": []}
    eligible = ["swarm1", "swarm2"]
    moves = plan_rebalance(per_node, eligible)
    assert len(moves) == 2
    assert _spread_after(per_node, eligible, moves) <= 1
    for _, src, tgt in moves:
        assert src == "swarm1" and tgt == "swarm2"


def test_new_empty_node_receives_hosts():
    # A freshly added node not present in placement should still be filled.
    per_node = {"swarm1": ["a", "b", "c", "d", "e", "f"]}
    eligible = ["swarm1", "swarm2"]  # swarm2 brand-new, zero hosts
    moves = plan_rebalance(per_node, eligible)
    assert _spread_after(per_node, eligible, moves) <= 1
    assert all(tgt == "swarm2" for _, _, tgt in moves)
    assert len(moves) == 3


def test_max_moves_caps_relocations():
    per_node = {"swarm1": ["a", "b", "c", "d", "e", "f"], "swarm2": []}
    eligible = ["swarm1", "swarm2"]
    moves = plan_rebalance(per_node, eligible, max_moves=1)
    assert len(moves) == 1


def test_drained_node_is_source_not_target():
    # swarm3 holds hosts but is not eligible (drained); its hosts should move
    # out and nothing should land on it.
    per_node = {"swarm1": ["a"], "swarm2": ["b"], "swarm3": ["c", "d", "e"]}
    eligible = ["swarm1", "swarm2"]
    moves = plan_rebalance(per_node, eligible)
    assert all(tgt in eligible for _, _, tgt in moves)
    assert any(src == "swarm3" for _, src, _ in moves)
    # Every host originally on the drained node should be relocated.
    moved_from_3 = [u for u, src, _ in moves if src == "swarm3"]
    assert set(moved_from_3) == {"c", "d", "e"}


def test_three_node_uneven_balances():
    per_node = {"swarm1": list("abcdef"), "swarm2": ["g"], "swarm3": []}
    eligible = ["swarm1", "swarm2", "swarm3"]
    moves = plan_rebalance(per_node, eligible)
    assert _spread_after(per_node, eligible, moves) <= 1


# ---------------------------------------------------------------------------
# Sprint 014 ticket 003: rebalance() still relocates a create-time-pinned
# host, via unpin -> move -> repin (`_pin_service_to_node()` replaces, not
# accumulates, any prior `node.hostname==` pin).
# ---------------------------------------------------------------------------

def _make_service(constraints: list[str], name: str = "svc") -> MagicMock:
    """Minimal mock Docker service double, mirroring test_node_unpin.py's
    helper of the same name (this file otherwise only exercises the pure
    `plan_rebalance()` function against plain dicts, so it has no docker
    service double of its own yet)."""
    svc = MagicMock()
    svc.name = name
    svc.attrs = {
        "Spec": {
            "TaskTemplate": {
                "Placement": {"Constraints": list(constraints)}
            }
        }
    }
    return svc


def _pin_via_real_call(svc: MagicMock, node_fqdn: str) -> None:
    """Pin `svc` using the real, unchanged `_pin_service_to_node()`, then bake
    the resulting constraint list back into `svc.attrs` so the fixture's
    constraint shape is guaranteed accurate (per the ticket's requirement)
    rather than hand-typed, and so later reads of `_service_constraints(svc)`
    see the pin as they would on a real Docker service post-update. Resets
    `svc.update`'s call history afterward so later assertions in a test only
    see the *next* `_pin_service_to_node()` call (the one under test)."""
    _pin_service_to_node(svc, node_fqdn)
    applied = list(svc.update.call_args.kwargs["constraints"])
    svc.attrs["Spec"]["TaskTemplate"]["Placement"]["Constraints"] = applied
    svc.update.reset_mock()


class TestPlanRebalanceIgnoresPinState:
    def test_move_plan_identical_regardless_of_backing_services_pin_state(self):
        """plan_rebalance() only ever consumes per_node (live task placement)
        and eligible -- never a service's existing constraints -- so its
        move-planning output for a given per_node/eligible input is identical
        no matter whether the affected host happens to be unpinned,
        rebalance-pinned, or create-time-pinned. Build all three kinds of
        service double to make that explicit, even though none of them are
        actually passed to plan_rebalance()."""
        per_node = {"swarm1": ["alice", "bob", "carol", "dave"], "swarm2": []}
        eligible = ["swarm1", "swarm2"]

        unpinned_moves = plan_rebalance(per_node, eligible)

        svc_rebalance_pinned = _make_service(["node.role != manager"], name="alice")
        _pin_service_to_node(svc_rebalance_pinned, "swarm1.example.com")
        rebalance_pinned_moves = plan_rebalance(per_node, eligible)

        svc_create_time_pinned = _make_service(["node.role != manager"], name="alice")
        _pin_via_real_call(svc_create_time_pinned, "swarm1.example.com")
        create_time_pinned_moves = plan_rebalance(per_node, eligible)

        assert unpinned_moves == rebalance_pinned_moves == create_time_pinned_moves
        assert len(unpinned_moves) == 2  # sanity: a real plan, not a no-op


class TestRebalanceRepinsCreateTimePinnedHost:
    """rebalance()'s per-move call is `_pin_service_to_node(svc, target_fqdn)`
    (cli/node.py:405-407) -- no separate "unpin" step, because
    `_pin_service_to_node()` itself already replaces any prior
    `node.hostname==` constraint. These tests mirror that exact call against
    a fixture pinned by Ticket 001's create-time path and check the result.
    """

    def test_rebalance_repins_create_time_pinned_host_without_accumulating(self):
        """After rebalance() moves a create-time-pinned host, the service
        carries exactly one node.hostname== constraint (the new target) --
        not two -- and its unrelated constraint survives."""
        svc_alice = _make_service(["node.role != manager"], name="alice")
        _pin_via_real_call(svc_alice, "swarm1.example.com")
        assert _service_constraints(svc_alice) == [
            "node.role != manager",
            "node.hostname==swarm1.example.com",
        ]

        # swarm1 vs swarm2 differ by 2, so the greedy planner moves exactly
        # one host (the most-recently-added, "alice") to level the spread.
        per_node = {"swarm1": ["bob", "alice"], "swarm2": []}
        eligible = ["swarm1", "swarm2"]
        fqdn_of_short = {"swarm1": "swarm1.example.com", "swarm2": "swarm2.example.com"}
        svc_by_user = {"alice": svc_alice}

        moves = plan_rebalance(per_node, eligible)
        assert moves == [("alice", "swarm1", "swarm2")]

        # Mirrors rebalance()'s per-move logic exactly (cli/node.py:405-407).
        for user, _src, tgt in moves:
            svc = svc_by_user[user]
            target_fqdn = fqdn_of_short.get(tgt, tgt)
            _pin_service_to_node(svc, target_fqdn)

        final_constraints = svc_alice.update.call_args.kwargs["constraints"]
        hostname_constraints = [
            c for c in final_constraints if c.replace(" ", "").startswith("node.hostname==")
        ]
        assert hostname_constraints == ["node.hostname==swarm2.example.com"]
        assert "node.role != manager" in final_constraints

    def test_repin_drops_old_target_and_preserves_unrelated_constraints(self):
        """The stale swarm1 pin is gone entirely (not merely superseded in
        ordering), and unrelated constraints besides node.role also survive
        the repin untouched."""
        svc = _make_service(
            ["node.role != manager", "node.labels.tier==standard"], name="bob"
        )
        _pin_via_real_call(svc, "swarm1.example.com")

        _pin_service_to_node(svc, "swarm3.example.com")

        final_constraints = svc.update.call_args.kwargs["constraints"]
        assert sorted(final_constraints) == sorted([
            "node.role != manager",
            "node.labels.tier==standard",
            "node.hostname==swarm3.example.com",
        ])
        assert "node.hostname==swarm1.example.com" not in final_constraints
