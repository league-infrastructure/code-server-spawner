"""
Unit tests for the `node rebalance` planner (plan_rebalance).

Pure-function tests for the move-planning logic — no Docker, DB, or network.
The greedy planner should level per-node host counts to a spread of at most
one, respect the eligible-node set, fill brand-new empty nodes, drain
non-eligible nodes, and honor max_moves.

Run with::

    uv run pytest test/test_node_rebalance.py -v
"""
from __future__ import annotations

from collections import Counter

from cspawn.cli.node import plan_rebalance


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
