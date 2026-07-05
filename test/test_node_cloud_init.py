"""Unit tests for sprint-009 ticket-001: ship cloud-init in the image, fail
loudly on configured-but-missing user-data.

Covers:
- `_resolve_cloud_init_path`: returns `None` when `DO_CLOUD_INIT`/
  `DO_CLOUD_INIT_FILE` are both unset; otherwise resolves
  `<project-root>/config/cloud-init/<file>` without checking existence.
- `_create_droplet`: configured + missing file -> `click.ClickException`,
  raised before any DigitalOcean side effect (`_ensure_priv_key`,
  `_collect_do_ssh_keys`, `digitalocean.Droplet(...)` are never called).
- `_create_droplet`: configured + file present -> file content passed
  through as the `user_data=` kwarg to the mocked `digitalocean.Droplet(...)`
  call.
- `_create_droplet`: unset config -> proceeds with `user_data=None`, no
  exception (regression guard for the explicit opt-out case).

Follows `test/test_node_unpin.py`'s MagicMock/`patch()` conventions and
`test/test_config.py`'s real-`tmp_path`-as-project-root convention (patching
`find_parent_dir` rather than mocking the filesystem).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest

from cspawn.cli.node import _create_droplet, _resolve_cloud_init_path


# ---------------------------------------------------------------------------
# _resolve_cloud_init_path
# ---------------------------------------------------------------------------

class TestResolveCloudInitPath:
    def test_returns_none_when_unset(self):
        """Neither DO_CLOUD_INIT nor DO_CLOUD_INIT_FILE set -> None."""
        assert _resolve_cloud_init_path({}) is None

    def test_returns_none_when_both_falsy(self):
        """Empty-string values are treated the same as unset."""
        cfg = {"DO_CLOUD_INIT": "", "DO_CLOUD_INIT_FILE": ""}
        assert _resolve_cloud_init_path(cfg) is None

    def test_resolves_path_from_do_cloud_init(self, tmp_path, monkeypatch):
        """DO_CLOUD_INIT resolves to <project-root>/config/cloud-init/<file>."""
        monkeypatch.setattr("cspawn.cli.node.find_parent_dir", lambda: tmp_path)

        result = _resolve_cloud_init_path({"DO_CLOUD_INIT": "swarm-node-init-v2.yaml"})

        assert result == tmp_path / "config" / "cloud-init" / "swarm-node-init-v2.yaml"

    def test_resolves_path_from_do_cloud_init_file(self, tmp_path, monkeypatch):
        """DO_CLOUD_INIT_FILE is an accepted alias when DO_CLOUD_INIT is unset."""
        monkeypatch.setattr("cspawn.cli.node.find_parent_dir", lambda: tmp_path)

        result = _resolve_cloud_init_path({"DO_CLOUD_INIT_FILE": "custom.yaml"})

        assert result == tmp_path / "config" / "cloud-init" / "custom.yaml"

    def test_do_cloud_init_takes_precedence_over_file(self, tmp_path, monkeypatch):
        """When both are set, DO_CLOUD_INIT wins."""
        monkeypatch.setattr("cspawn.cli.node.find_parent_dir", lambda: tmp_path)

        result = _resolve_cloud_init_path(
            {"DO_CLOUD_INIT": "primary.yaml", "DO_CLOUD_INIT_FILE": "fallback.yaml"}
        )

        assert result == tmp_path / "config" / "cloud-init" / "primary.yaml"

    def test_does_not_check_existence(self, tmp_path, monkeypatch):
        """The resolved path is returned even though nothing was written to disk."""
        monkeypatch.setattr("cspawn.cli.node.find_parent_dir", lambda: tmp_path)

        result = _resolve_cloud_init_path({"DO_CLOUD_INIT": "does-not-exist.yaml"})

        assert result is not None
        assert not result.exists()


# ---------------------------------------------------------------------------
# _create_droplet: cloud-init resolution
# ---------------------------------------------------------------------------

def _invoke_create_droplet(tmp_path, cfg: dict, *, droplet_cls: MagicMock):
    """Call `_create_droplet` with the DigitalOcean/SSH/network surface mocked
    out, but real `tmp_path`-backed cloud-init path resolution (per
    test_config.py's convention: patch `find_parent_dir`, don't mock the fs).

    Returns (result_or_None, exception_or_None, mocks) so callers can assert
    on both the happy path and the failure path with one helper.
    """
    mgr = MagicMock()
    mgr.get_all_droplets.return_value = []
    manager_client = MagicMock()

    mock_ensure_priv_key = MagicMock(return_value=(Path("/fake/id_rsa"), Path("/fake/id_rsa.pub")))
    mock_collect_ssh_keys = MagicMock(return_value=[])
    mock_wait_active = MagicMock(return_value="10.0.0.5")
    mock_find_manager_droplet = MagicMock(return_value=None)

    with (
        patch("cspawn.cli.node.get_config", return_value=cfg),
        patch("cspawn.cli.node.get_logger", return_value=MagicMock()),
        patch("cspawn.cli.node.find_parent_dir", return_value=tmp_path),
        patch("cspawn.cli.node._ensure_priv_key", mock_ensure_priv_key),
        patch("cspawn.cli.node._collect_do_ssh_keys", mock_collect_ssh_keys),
        patch("cspawn.cli.node.digitalocean.Droplet", droplet_cls),
        patch("cspawn.cli.node._wait_for_droplet_active", mock_wait_active),
        patch("cspawn.cli.node._find_manager_droplet", mock_find_manager_droplet),
    ):
        exc = None
        result = None
        try:
            result = _create_droplet(
                ctx=MagicMock(),
                mgr=mgr,
                manager_client=manager_client,
                name_template="swarm{serial}.example.com",
                do_token="fake-token",
                do_region="nyc3",
                do_size="s-1vcpu-2gb",
                do_image="docker-20-04",
                project_selector=None,
                desired_serial=5,
                docker_uri="ssh://root@manager.example.com",
                do_tag=None,
                tier=None,
            )
        except click.ClickException as e:
            exc = e

    mocks = {
        "ensure_priv_key": mock_ensure_priv_key,
        "collect_ssh_keys": mock_collect_ssh_keys,
        "droplet_cls": droplet_cls,
    }
    return result, exc, mocks


class TestCreateDropletCloudInitMissing:
    def test_missing_file_raises_click_exception_before_any_do_side_effect(self, tmp_path):
        """Configured + missing file: raises before SSH-key prep or Droplet()."""
        cfg = {"DO_CLOUD_INIT": "swarm-node-init-v2.yaml"}
        droplet_cls = MagicMock()

        result, exc, mocks = _invoke_create_droplet(tmp_path, cfg, droplet_cls=droplet_cls)

        assert result is None
        assert exc is not None
        expected_path = tmp_path / "config" / "cloud-init" / "swarm-node-init-v2.yaml"
        assert str(expected_path) in exc.format_message()

        mocks["ensure_priv_key"].assert_not_called()
        mocks["collect_ssh_keys"].assert_not_called()
        mocks["droplet_cls"].assert_not_called()

    def test_missing_file_via_do_cloud_init_file_alias(self, tmp_path):
        """Same fail-loud behavior when configured via the DO_CLOUD_INIT_FILE alias."""
        cfg = {"DO_CLOUD_INIT_FILE": "swarm-node-init-v2.yaml"}
        droplet_cls = MagicMock()

        result, exc, mocks = _invoke_create_droplet(tmp_path, cfg, droplet_cls=droplet_cls)

        assert result is None
        assert exc is not None
        mocks["droplet_cls"].assert_not_called()

    def test_unreadable_file_raises_click_exception_before_any_do_side_effect(self, tmp_path):
        """Configured + present-but-unreadable file: same fail-loud contract as missing."""
        cloud_init_dir = tmp_path / "config" / "cloud-init"
        cloud_init_dir.mkdir(parents=True)
        unreadable = cloud_init_dir / "swarm-node-init-v2.yaml"
        unreadable.write_text("#cloud-config\n")
        unreadable.chmod(0o000)
        cfg = {"DO_CLOUD_INIT": "swarm-node-init-v2.yaml"}
        droplet_cls = MagicMock()

        try:
            result, exc, mocks = _invoke_create_droplet(tmp_path, cfg, droplet_cls=droplet_cls)
        finally:
            unreadable.chmod(0o644)

        assert result is None
        assert exc is not None
        assert str(unreadable) in exc.format_message()
        mocks["ensure_priv_key"].assert_not_called()
        mocks["collect_ssh_keys"].assert_not_called()
        mocks["droplet_cls"].assert_not_called()


class TestCreateDropletCloudInitFound:
    def test_found_file_content_passed_as_user_data(self, tmp_path):
        """Configured + file present: content is read and passed as user_data=."""
        cloud_init_dir = tmp_path / "config" / "cloud-init"
        cloud_init_dir.mkdir(parents=True)
        cloud_init_content = "#cloud-config\nruncmd:\n  - echo hello\n"
        (cloud_init_dir / "swarm-node-init-v2.yaml").write_text(cloud_init_content)

        cfg = {"DO_CLOUD_INIT": "swarm-node-init-v2.yaml"}
        mock_instance = MagicMock()
        droplet_cls = MagicMock(return_value=mock_instance)

        result, exc, mocks = _invoke_create_droplet(tmp_path, cfg, droplet_cls=droplet_cls)

        assert exc is None
        assert result is not None
        mocks["droplet_cls"].assert_called_once()
        _, kwargs = mocks["droplet_cls"].call_args
        assert kwargs["user_data"] == cloud_init_content

    def test_found_file_does_not_short_circuit_ssh_key_prep(self, tmp_path):
        """Configured + file present: SSH-key prep still happens (unchanged path)."""
        cloud_init_dir = tmp_path / "config" / "cloud-init"
        cloud_init_dir.mkdir(parents=True)
        (cloud_init_dir / "swarm-node-init-v2.yaml").write_text("#cloud-config\n")

        cfg = {"DO_CLOUD_INIT": "swarm-node-init-v2.yaml"}
        droplet_cls = MagicMock(return_value=MagicMock())

        _, exc, mocks = _invoke_create_droplet(tmp_path, cfg, droplet_cls=droplet_cls)

        assert exc is None
        mocks["ensure_priv_key"].assert_called_once()
        mocks["collect_ssh_keys"].assert_called_once()


class TestCreateDropletCloudInitUnset:
    def test_unset_config_proceeds_with_user_data_none(self, tmp_path):
        """No DO_CLOUD_INIT/DO_CLOUD_INIT_FILE: proceeds with user_data=None, no exception."""
        cfg = {}
        mock_instance = MagicMock()
        droplet_cls = MagicMock(return_value=mock_instance)

        result, exc, mocks = _invoke_create_droplet(tmp_path, cfg, droplet_cls=droplet_cls)

        assert exc is None
        assert result is not None
        mocks["droplet_cls"].assert_called_once()
        _, kwargs = mocks["droplet_cls"].call_args
        assert kwargs["user_data"] is None
