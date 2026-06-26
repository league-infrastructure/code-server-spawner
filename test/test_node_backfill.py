"""Unit tests for `cspawnctl node label-backfill`.

All tests use mocked Docker and DigitalOcean clients — no live provisioning.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, call

import pytest
from click.testing import CliRunner

from cspawn.cli.node import label_backfill


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

NODE_TIERS_JSON = json.dumps([
    {"name": "small", "slug": "s-4vcpu-8gb-amd", "capacity": 6},
    {"name": "large", "slug": "s-8vcpu-16gb-amd", "capacity": 14},
])

BASE_CFG = {
    "NODE_TIERS": NODE_TIERS_JSON,
    "DOCKER_URI": "ssh://fake-manager",
    "DO_TOKEN": "fake-token",
    "DO_NAMES": "swarm{serial}.example.com",
    "DO_TAG": "cspawn",
}


def _make_swarm_node(hostname: str, spec_labels: dict | None = None) -> MagicMock:
    """Build a minimal mock swarm node object."""
    node = MagicMock()
    node.attrs = {
        "Description": {"Hostname": hostname},
        "Spec": {"Labels": dict(spec_labels) if spec_labels else {}},
    }
    return node


def _make_droplet(name: str, size_slug: str) -> MagicMock:
    """Build a minimal mock DO droplet object."""
    d = MagicMock()
    d.name = name
    d.size_slug = size_slug
    return d


def _invoke(args: list[str], cfg: dict | None = None):
    """Invoke the label-backfill command with mocked infrastructure."""
    if cfg is None:
        cfg = BASE_CFG

    runner = CliRunner(mix_stderr=False)

    with patch("cspawn.cli.node.get_config", return_value=cfg), \
         patch("cspawn.cli.node.get_logger", return_value=MagicMock()):
        result = runner.invoke(label_backfill, args, obj={"v": 0, "deploy": "devel"},
                               catch_exceptions=False)

    return result


# ---------------------------------------------------------------------------
# Helper: set up patched docker + DO clients for a given scenario
# ---------------------------------------------------------------------------

def _run_with_mocks(
    args: list[str],
    swarm_nodes: list,
    droplets: list,
    cfg: dict | None = None,
):
    """Run label-backfill with fully mocked docker + DO layer."""
    if cfg is None:
        cfg = BASE_CFG

    mock_docker_client = MagicMock()
    mock_docker_client.nodes.list.return_value = swarm_nodes

    # _ensure_node_labels uses inspect_node + update_node
    mock_docker_client.api.inspect_node.return_value = {
        "Version": {"Index": 42},
        "Spec": {"Labels": {}},
    }
    mock_docker_client.api.update_node.return_value = None

    mock_do_mgr = MagicMock()
    mock_do_mgr.get_all_droplets.return_value = droplets

    runner = CliRunner(mix_stderr=False)

    with patch("cspawn.cli.node.get_config", return_value=cfg), \
         patch("cspawn.cli.node.get_logger", return_value=MagicMock()), \
         patch("cspawn.cli.node.docker.DockerClient", return_value=mock_docker_client), \
         patch("cspawn.cli.node.digitalocean.Manager", return_value=mock_do_mgr):

        result = runner.invoke(
            label_backfill, args,
            obj={"v": 0, "deploy": "devel"},
            catch_exceptions=False,
        )

    return result, mock_docker_client, mock_do_mgr


# ---------------------------------------------------------------------------
# Test: dry-run prints table without writing labels
# ---------------------------------------------------------------------------

def test_label_backfill_dry_run_prints_table_without_writing():
    """Dry-run (no --apply) prints the table but does not call update_node."""
    nodes = [_make_swarm_node("swarm2.example.com")]
    droplets = [_make_droplet("swarm2", "s-4vcpu-8gb-amd")]

    result, docker_client, _ = _run_with_mocks([], nodes, droplets)

    assert result.exit_code == 0, result.output
    assert "would-apply" in result.output
    assert "swarm2" in result.output
    assert "small" in result.output
    assert "Dry run" in result.output
    docker_client.api.update_node.assert_not_called()


# ---------------------------------------------------------------------------
# Test: --apply stamps labels on unlabeled nodes
# ---------------------------------------------------------------------------

def test_label_backfill_apply_stamps_labels():
    """With --apply, _ensure_node_labels is called with correct tier labels."""
    nodes = [_make_swarm_node("swarm3.example.com")]
    droplets = [_make_droplet("swarm3", "s-8vcpu-16gb-amd")]

    result, docker_client, _ = _run_with_mocks(["--apply"], nodes, droplets)

    assert result.exit_code == 0, result.output
    assert "applied" in result.output

    # update_node must have been called (via _ensure_node_labels)
    docker_client.api.update_node.assert_called_once()
    call_args = docker_client.api.update_node.call_args
    updated_spec = call_args[0][2]
    assert updated_spec["Labels"]["cs.tier"] == "large"
    assert updated_spec["Labels"]["cs.capacity"] == "14"


# ---------------------------------------------------------------------------
# Test: already-labeled nodes are skipped
# ---------------------------------------------------------------------------

def test_label_backfill_skips_already_labeled_nodes():
    """Nodes that already have cs.tier set show 'already-set'; update_node is NOT called."""
    nodes = [_make_swarm_node("swarm1.example.com",
                               spec_labels={"cs.tier": "small", "cs.capacity": "6"})]
    droplets = [_make_droplet("swarm1", "s-4vcpu-8gb-amd")]

    result, docker_client, _ = _run_with_mocks(["--apply"], nodes, droplets)

    assert result.exit_code == 0, result.output
    assert "already-set" in result.output
    docker_client.api.update_node.assert_not_called()


# ---------------------------------------------------------------------------
# Test: unknown slug → WARN in ACTION column
# ---------------------------------------------------------------------------

def test_label_backfill_unknown_slug_shows_warning():
    """When a node's droplet slug is not in NODE_TIERS, ACTION shows 'WARN: unknown slug'."""
    nodes = [_make_swarm_node("swarm4.example.com")]
    droplets = [_make_droplet("swarm4", "s-very-large-unknown")]

    result, docker_client, _ = _run_with_mocks([], nodes, droplets)

    assert result.exit_code == 0, result.output
    assert "WARN: unknown slug" in result.output
    docker_client.api.update_node.assert_not_called()


# ---------------------------------------------------------------------------
# Test: idempotent — second --apply is a no-op
# ---------------------------------------------------------------------------

def test_label_backfill_idempotent_on_second_apply():
    """Second --apply: node already has cs.tier → shows 'already-set', no update_node call."""
    # First call: node has no labels → gets stamped
    nodes_unlabeled = [_make_swarm_node("swarm5.example.com")]
    droplets = [_make_droplet("swarm5", "s-4vcpu-8gb-amd")]

    result1, docker_client1, _ = _run_with_mocks(["--apply"], nodes_unlabeled, droplets)
    assert result1.exit_code == 0
    assert "applied" in result1.output

    # Second call: node now has labels → already-set, no update
    nodes_labeled = [_make_swarm_node("swarm5.example.com",
                                       spec_labels={"cs.tier": "small", "cs.capacity": "6"})]
    result2, docker_client2, _ = _run_with_mocks(["--apply"], nodes_labeled, droplets)
    assert result2.exit_code == 0
    assert "already-set" in result2.output
    docker_client2.api.update_node.assert_not_called()


# ---------------------------------------------------------------------------
# Test: table always printed (even in dry-run)
# ---------------------------------------------------------------------------

def test_label_backfill_table_columns_present():
    """Output always contains the expected column headers."""
    nodes = [_make_swarm_node("swarm2.example.com")]
    droplets = [_make_droplet("swarm2", "s-4vcpu-8gb-amd")]

    result, _, _ = _run_with_mocks([], nodes, droplets)

    assert result.exit_code == 0
    assert "NODE" in result.output
    assert "SIZE_SLUG" in result.output
    assert "INFERRED_TIER" in result.output
    assert "CAPACITY" in result.output
    assert "ACTION" in result.output


# ---------------------------------------------------------------------------
# Test: nodes not matching DO_NAMES pattern are ignored
# ---------------------------------------------------------------------------

def test_label_backfill_ignores_non_matching_nodes():
    """Nodes whose hostname doesn't match DO_NAMES template are not included in table."""
    nodes = [
        _make_swarm_node("other-host.example.com"),   # does not match swarm{serial}
        _make_swarm_node("swarm1.example.com"),
    ]
    droplets = [_make_droplet("swarm1", "s-4vcpu-8gb-amd")]

    result, _, _ = _run_with_mocks([], nodes, droplets)

    assert result.exit_code == 0
    assert "other-host" not in result.output
    assert "swarm1" in result.output


# ---------------------------------------------------------------------------
# Test: missing DO_TOKEN raises ClickException
# ---------------------------------------------------------------------------

def test_label_backfill_missing_do_token_raises():
    """Missing DO_TOKEN raises a ClickException with a clear message."""
    cfg_no_token = {k: v for k, v in BASE_CFG.items() if k != "DO_TOKEN"}

    runner = CliRunner()  # mix_stderr=True so ClickException message lands in .output
    with patch("cspawn.cli.node.get_config", return_value=cfg_no_token), \
         patch("cspawn.cli.node.get_logger", return_value=MagicMock()):
        result = runner.invoke(
            label_backfill, [],
            obj={"v": 0, "deploy": "devel"},
            catch_exceptions=False,
        )

    assert result.exit_code != 0
    assert "DO_TOKEN" in result.output


# ---------------------------------------------------------------------------
# Test: missing DOCKER_URI raises ClickException
# ---------------------------------------------------------------------------

def test_label_backfill_missing_docker_uri_raises():
    """Missing DOCKER_URI raises a ClickException with a clear message."""
    cfg_no_docker = {k: v for k, v in BASE_CFG.items() if k != "DOCKER_URI"}

    runner = CliRunner()  # mix_stderr=True so ClickException message lands in .output
    with patch("cspawn.cli.node.get_config", return_value=cfg_no_docker), \
         patch("cspawn.cli.node.get_logger", return_value=MagicMock()):
        result = runner.invoke(
            label_backfill, [],
            obj={"v": 0, "deploy": "devel"},
            catch_exceptions=False,
        )

    assert result.exit_code != 0
    assert "DOCKER_URI" in result.output or "DO_NAMES" in result.output
