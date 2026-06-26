"""Unit tests for _ensure_node_labels and size-aware expand/join wiring.

Tests use mocked Docker and DigitalOcean clients — no live provisioning.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, call, patch

import pytest
from click.testing import CliRunner

from cspawn.cs_docker.tiers import Tier
from cspawn.cli.node import _ensure_node_labels


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NODE_TIERS_JSON = json.dumps([
    {"name": "small", "slug": "s-4vcpu-8gb-amd", "capacity": 6},
    {"name": "large", "slug": "s-8vcpu-16gb-amd", "capacity": 14},
])


def _make_manager_client(node_id="node-abc", node_hostname="worker1",
                         existing_labels=None, version_index=42):
    """Build a minimal mock docker.DockerClient with swarm node list/inspect/update."""
    client = MagicMock()

    # Build the node mock
    node_mock = MagicMock()
    node_mock.id = node_id
    node_mock.attrs = {
        "Description": {"Hostname": node_hostname},
    }

    client.nodes.list.return_value = [node_mock]

    # inspect_node returns structure the real Docker API returns
    client.api.inspect_node.return_value = {
        "Version": {"Index": version_index},
        "Spec": {
            "Labels": dict(existing_labels) if existing_labels else {},
        },
        "Description": {"Hostname": node_hostname},
        "Status": {"Addr": "10.0.0.1"},
    }
    client.api.update_node.return_value = None

    return client


# ---------------------------------------------------------------------------
# _ensure_node_labels: apply all keys
# ---------------------------------------------------------------------------


def test_ensure_node_labels_applies_all_keys():
    """When no labels exist, update_node is called with all provided labels."""
    client = _make_manager_client(existing_labels={})

    result = _ensure_node_labels(client, "worker1", {"cs.tier": "small", "cs.capacity": "6"})

    assert result is True
    client.api.update_node.assert_called_once()
    call_args = client.api.update_node.call_args
    updated_spec = call_args[0][2]  # positional arg: spec
    assert updated_spec["Labels"]["cs.tier"] == "small"
    assert updated_spec["Labels"]["cs.capacity"] == "6"


# ---------------------------------------------------------------------------
# _ensure_node_labels: skip when already set
# ---------------------------------------------------------------------------


def test_ensure_node_labels_skips_if_already_set():
    """When all labels already have correct values, update_node is NOT called."""
    client = _make_manager_client(existing_labels={"cs.tier": "small", "cs.capacity": "6"})

    result = _ensure_node_labels(client, "worker1", {"cs.tier": "small", "cs.capacity": "6"})

    assert result is False
    client.api.update_node.assert_not_called()


# ---------------------------------------------------------------------------
# _ensure_node_labels: partial update
# ---------------------------------------------------------------------------


def test_ensure_node_labels_partial_update():
    """Only the missing label is added; the already-correct one is preserved."""
    # cs.tier already correct, cs.capacity missing
    client = _make_manager_client(existing_labels={"cs.tier": "small"})

    result = _ensure_node_labels(client, "worker1", {"cs.tier": "small", "cs.capacity": "6"})

    assert result is True
    client.api.update_node.assert_called_once()
    call_args = client.api.update_node.call_args
    updated_spec = call_args[0][2]
    # Both labels present in final spec
    assert updated_spec["Labels"]["cs.tier"] == "small"
    assert updated_spec["Labels"]["cs.capacity"] == "6"


def test_ensure_node_labels_partial_update_overwrites_changed_value():
    """If a label exists but has a different value, it is overwritten."""
    client = _make_manager_client(existing_labels={"cs.tier": "old-value", "cs.capacity": "6"})

    result = _ensure_node_labels(client, "worker1", {"cs.tier": "small", "cs.capacity": "6"})

    assert result is True
    client.api.update_node.assert_called_once()
    call_args = client.api.update_node.call_args
    updated_spec = call_args[0][2]
    assert updated_spec["Labels"]["cs.tier"] == "small"


# ---------------------------------------------------------------------------
# _ensure_node_labels: returns False on error
# ---------------------------------------------------------------------------


def test_ensure_node_labels_returns_false_on_inspect_error():
    """If inspect_node raises, the function catches and returns False."""
    client = _make_manager_client()
    client.api.inspect_node.side_effect = RuntimeError("connection refused")

    result = _ensure_node_labels(client, "worker1", {"cs.tier": "small"})

    assert result is False
    client.api.update_node.assert_not_called()


def test_ensure_node_labels_returns_false_on_update_error():
    """If update_node raises, the function catches and returns False."""
    client = _make_manager_client(existing_labels={})
    client.api.update_node.side_effect = RuntimeError("swarm update failed")

    result = _ensure_node_labels(client, "worker1", {"cs.tier": "small"})

    assert result is False


def test_ensure_node_labels_returns_false_when_node_not_found():
    """If the node is not in the swarm node list, returns False gracefully."""
    client = _make_manager_client(node_hostname="other-node")
    # Looking for "worker1" but swarm only has "other-node"

    result = _ensure_node_labels(client, "worker1", {"cs.tier": "small"})

    assert result is False
    client.api.update_node.assert_not_called()


def test_ensure_node_labels_empty_labels_dict():
    """An empty labels dict is a no-op — returns False without calling update."""
    client = _make_manager_client()

    result = _ensure_node_labels(client, "worker1", {})

    assert result is False
    client.api.update_node.assert_not_called()


def test_ensure_node_labels_logs_applied_labels(caplog):
    """When log is provided, applied labels are logged."""
    import logging

    client = _make_manager_client(existing_labels={})
    log = logging.getLogger("test")

    with caplog.at_level(logging.INFO, logger="test"):
        _ensure_node_labels(client, "worker1", {"cs.tier": "small"}, log=log)

    assert any("cs.tier=small" in r.message for r in caplog.records)


def test_ensure_node_labels_logs_warning_on_error(caplog):
    """When log is provided and an error occurs, a warning is logged."""
    import logging

    client = _make_manager_client()
    client.api.inspect_node.side_effect = RuntimeError("boom")
    log = logging.getLogger("test")

    with caplog.at_level(logging.WARNING, logger="test"):
        result = _ensure_node_labels(client, "worker1", {"cs.tier": "small"}, log=log)

    assert result is False
    assert any("Failed to apply node labels" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Tier resolution: tier_by_name / default_tier / ClickException
# ---------------------------------------------------------------------------


def test_tier_by_name_returns_correct_tier():
    """tier_by_name returns the matching Tier from a config with NODE_TIERS."""
    from cspawn.cs_docker.tiers import tier_by_name, load_tiers

    cfg = {"NODE_TIERS": NODE_TIERS_JSON}
    t = tier_by_name(cfg, "large")
    assert t is not None
    assert t.slug == "s-8vcpu-16gb-amd"
    assert t.capacity == 14


def test_tier_by_name_returns_none_for_unknown():
    """tier_by_name returns None for an unknown tier name."""
    from cspawn.cs_docker.tiers import tier_by_name

    cfg = {"NODE_TIERS": NODE_TIERS_JSON}
    assert tier_by_name(cfg, "xlarge") is None


def test_expand_unknown_tier_raises_click_exception():
    """expand --tier <unknown> raises ClickException listing valid tier names."""
    from cspawn.cli.node import expand

    runner = CliRunner()
    # We can't connect to a real docker/DO, but the tier validation happens
    # before any network calls — so the exception fires first.
    with patch("cspawn.cli.node.get_config") as mock_cfg, \
         patch("cspawn.cli.node.get_logger") as mock_log:
        mock_cfg.return_value = {
            "NODE_TIERS": NODE_TIERS_JSON,
            "DO_TOKEN": "fake-token",
            "DO_NAMES": "node{serial}.example.com",
            "DOCKER_URI": "ssh://fake-host",
        }
        mock_log.return_value = MagicMock()

        result = runner.invoke(expand, ["--tier", "xlarge"])

    assert result.exit_code != 0
    assert "xlarge" in result.output
    # Valid tier names should appear in the error message
    assert "small" in result.output or "large" in result.output


def test_expand_default_tier_used_when_no_tier_flag():
    """expand without --tier resolves to default_tier (first tier or DEFAULT_TIER)."""
    from cspawn.cs_docker.tiers import default_tier

    cfg = {"NODE_TIERS": NODE_TIERS_JSON, "DEFAULT_TIER": "large"}
    t = default_tier(cfg)
    assert t.name == "large"
    assert t.slug == "s-8vcpu-16gb-amd"


# ---------------------------------------------------------------------------
# _ensure_node_labels: version index is passed through to update_node
# ---------------------------------------------------------------------------


def test_ensure_node_labels_passes_version_to_update():
    """The version index from inspect_node is forwarded to update_node."""
    client = _make_manager_client(existing_labels={}, version_index=99)

    _ensure_node_labels(client, "worker1", {"cs.tier": "small"})

    call_args = client.api.update_node.call_args
    version_arg = call_args[0][1]  # positional arg: version
    assert version_arg == 99


# ---------------------------------------------------------------------------
# _ensure_node_labels: existing unrelated labels are preserved
# ---------------------------------------------------------------------------


def test_ensure_node_labels_preserves_existing_unrelated_labels():
    """Existing labels not in the update dict are preserved in the merged spec."""
    client = _make_manager_client(existing_labels={"code-host-user": "true"})

    _ensure_node_labels(client, "worker1", {"cs.tier": "small", "cs.capacity": "6"})

    call_args = client.api.update_node.call_args
    updated_spec = call_args[0][2]
    # Original label must still be present
    assert updated_spec["Labels"]["code-host-user"] == "true"
    assert updated_spec["Labels"]["cs.tier"] == "small"
