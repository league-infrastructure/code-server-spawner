"""Unit tests for sprint-009 ticket-002: post-join provisioning verification
in `cspawnctl node expand`.

Covers:
- `_expected_docker_version`: parses the `DOCKER_PIN="5:X.Y.Z-..."` pattern
  from the resolved cloud-init file; returns `None` (never raises) when
  unconfigured, the file is missing, or the pattern isn't found.
- `_verify_node_provisioning`: three independent SSH-based checks (connect
  reachability, docker version, cloud-init status) aggregated into a
  failure-string list; empty list means healthy; never raises for an
  expected failure mode.
- `expand()` CLI wiring: post-join verification failure drains the node and
  aborts with a non-zero exit; success leaves the pre-existing summary
  output unchanged.

Follows `test/test_node_cloud_init.py`'s `find_parent_dir`-patch /
`tmp_path`-as-project-root convention and `test/test_node_labels.py`'s
`get_config`/`get_logger` CliRunner mocking convention.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cspawn.cli.node import (
    _expected_docker_version,
    _major,
    _manager_docker_version,
    _verify_node_provisioning,
    expand,
)


# ---------------------------------------------------------------------------
# _expected_docker_version
# ---------------------------------------------------------------------------

CLOUD_INIT_WITH_PIN = (
    "#cloud-config\n"
    "runcmd:\n"
    "  - apt-get update -qq\n"
    "  - >-\n"
    "    . /etc/os-release;\n"
    '    DOCKER_PIN="5:29.6.1-1~ubuntu.${VERSION_ID}~${VERSION_CODENAME}";\n'
    "    apt-get install -y --allow-downgrades --allow-change-held-packages\n"
    '    "docker-ce=${DOCKER_PIN}" "docker-ce-cli=${DOCKER_PIN}"\n'
    "  - apt-mark hold docker-ce docker-ce-cli\n"
)

CLOUD_INIT_WITHOUT_PIN = "#cloud-config\nruncmd:\n  - echo hello\n"


class TestExpectedDockerVersion:
    def test_returns_none_when_unconfigured(self):
        """No DO_CLOUD_INIT/DO_CLOUD_INIT_FILE -> None, no file access attempted."""
        assert _expected_docker_version({}) is None

    def test_parses_pin_from_configured_file(self, tmp_path, monkeypatch):
        """DOCKER_PIN="5:X.Y.Z-..." is parsed into "X.Y.Z"."""
        monkeypatch.setattr("cspawn.cli.node.find_parent_dir", lambda: tmp_path)
        cloud_init_dir = tmp_path / "config" / "cloud-init"
        cloud_init_dir.mkdir(parents=True)
        (cloud_init_dir / "swarm-node-init-v2.yaml").write_text(CLOUD_INIT_WITH_PIN)

        result = _expected_docker_version({"DO_CLOUD_INIT": "swarm-node-init-v2.yaml"})

        assert result == "29.6.1"

    def test_returns_none_when_file_missing(self, tmp_path, monkeypatch):
        """Configured but the resolved file doesn't exist -> None, not an error."""
        monkeypatch.setattr("cspawn.cli.node.find_parent_dir", lambda: tmp_path)

        result = _expected_docker_version({"DO_CLOUD_INIT": "does-not-exist.yaml"})

        assert result is None

    def test_returns_none_when_pattern_not_found(self, tmp_path, monkeypatch):
        """File exists but has no DOCKER_PIN line -> None, not an error."""
        monkeypatch.setattr("cspawn.cli.node.find_parent_dir", lambda: tmp_path)
        cloud_init_dir = tmp_path / "config" / "cloud-init"
        cloud_init_dir.mkdir(parents=True)
        (cloud_init_dir / "swarm-node-init-v2.yaml").write_text(CLOUD_INIT_WITHOUT_PIN)

        result = _expected_docker_version({"DO_CLOUD_INIT": "swarm-node-init-v2.yaml"})

        assert result is None

    def test_returns_none_when_unreadable(self, tmp_path, monkeypatch):
        """File exists but can't be read -> None, not an error."""
        monkeypatch.setattr("cspawn.cli.node.find_parent_dir", lambda: tmp_path)
        cloud_init_dir = tmp_path / "config" / "cloud-init"
        cloud_init_dir.mkdir(parents=True)
        unreadable = cloud_init_dir / "swarm-node-init-v2.yaml"
        unreadable.write_text(CLOUD_INIT_WITH_PIN)
        unreadable.chmod(0o000)

        try:
            result = _expected_docker_version({"DO_CLOUD_INIT": "swarm-node-init-v2.yaml"})
        finally:
            unreadable.chmod(0o644)

        assert result is None


# ---------------------------------------------------------------------------
# _manager_docker_version
# ---------------------------------------------------------------------------

class TestManagerDockerVersion:
    """Direct unit coverage for the live manager-version query helper, shared
    by `_join_swarm`'s pre-join preflight, `_create_droplet`'s cloud-init
    `__DOCKER_VERSION__` substitution, and `expand`'s post-join verification.
    """

    def test_returns_version_field_from_manager_client(self):
        manager_client = MagicMock()
        manager_client.version.return_value = {"Version": "29.7.2", "ApiVersion": "1.51"}

        assert _manager_docker_version(manager_client) == "29.7.2"

    def test_returns_none_when_version_call_raises(self):
        manager_client = MagicMock()
        manager_client.version.side_effect = Exception("connection refused")

        assert _manager_docker_version(manager_client) is None

    def test_returns_none_when_version_field_missing(self):
        manager_client = MagicMock()
        manager_client.version.return_value = {"ApiVersion": "1.51"}

        assert _manager_docker_version(manager_client) is None

    def test_returns_none_when_version_call_returns_none(self):
        manager_client = MagicMock()
        manager_client.version.return_value = None

        assert _manager_docker_version(manager_client) is None


# ---------------------------------------------------------------------------
# _major
# ---------------------------------------------------------------------------

class TestMajor:
    """Direct unit coverage for the module-level `_major` helper, shared by
    `_join_swarm`'s pre-join preflight and `_verify_node_provisioning`'s
    post-join docker-version check."""

    def test_bare_pinned_version_string(self):
        """A "29.6.1"-shaped pinned version yields its leading integer."""
        assert _major("29.6.1") == 29

    def test_free_form_docker_version_output(self):
        """A "Docker version 29.6.0, build abc"-shaped string still yields 29."""
        assert _major("Docker version 29.6.0, build abc") == 29

    def test_none_input_returns_none(self):
        assert _major(None) is None

    def test_unparseable_input_returns_none(self):
        assert _major("not a version at all") is None


# ---------------------------------------------------------------------------
# _verify_node_provisioning
# ---------------------------------------------------------------------------

def _make_ssh_exec(ssh_connect_results=None, docker_output="Docker version 29.6.1, build abc123",
                    cloud_init_output="status: done"):
    """Build a fake `_ssh_exec(host, username, key_path, cmd, **kwargs)`.

    `ssh_connect_results` is a list of bool per consecutive "true" (connect
    check) call; extra calls beyond the list default to success. `cmd` values
    other than "true" are routed to the docker-version / cloud-init-status
    canned outputs regardless of call order.
    """
    ssh_connect_results = list(ssh_connect_results) if ssh_connect_results is not None else []
    state = {"connect_call": 0}

    def _fake(host, username, key_path, cmd, **kwargs):
        if cmd == "true":
            idx = state["connect_call"]
            state["connect_call"] += 1
            ok = ssh_connect_results[idx] if idx < len(ssh_connect_results) else True
            if not ok:
                raise Exception("simulated SSH connect failure")
            return (0, "", "")
        elif cmd == "docker --version":
            return (0, docker_output, "")
        elif cmd == "cloud-init status":
            return (0, cloud_init_output, "")
        raise AssertionError(f"unexpected command: {cmd!r}")

    return _fake


class TestVerifyNodeProvisioning:
    def test_all_checks_pass_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr("cspawn.cli.node._ssh_exec", _make_ssh_exec())

        result = _verify_node_provisioning(
            "10.0.0.5", Path("/fake/id_rsa"),
            expected_docker_version="29.6.1", retry_delay=0,
        )

        assert result == []

    def test_flaky_ssh_reports_success_count(self, monkeypatch):
        """2 of 3 consecutive connects succeed -> failure names the count."""
        monkeypatch.setattr(
            "cspawn.cli.node._ssh_exec",
            _make_ssh_exec(ssh_connect_results=[True, False, True]),
        )

        result = _verify_node_provisioning(
            "10.0.0.5", Path("/fake/id_rsa"),
            expected_docker_version="29.6.1", ssh_checks=3, retry_delay=0,
        )

        assert len(result) == 1
        assert "2/3" in result[0]

    def test_docker_version_mismatch_reports_both_values(self, monkeypatch):
        """A genuine major-version mismatch (29 vs. 28) is a failure naming both
        full version strings."""
        monkeypatch.setattr(
            "cspawn.cli.node._ssh_exec",
            _make_ssh_exec(docker_output="Docker version 28.4.0, build xyz"),
        )

        result = _verify_node_provisioning(
            "10.0.0.5", Path("/fake/id_rsa"),
            expected_docker_version="29.6.1", retry_delay=0,
        )

        assert len(result) == 1
        assert "29.6.1" in result[0]
        assert "28.4.0" in result[0]

    def test_docker_version_patch_difference_passes(self, monkeypatch):
        """Same major, different patch (29.6.0 vs. expected 29.6.1) is not a
        failure: Docker Swarm only requires major-version compatibility."""
        monkeypatch.setattr(
            "cspawn.cli.node._ssh_exec",
            _make_ssh_exec(docker_output="Docker version 29.6.0, build xyz"),
        )

        result = _verify_node_provisioning(
            "10.0.0.5", Path("/fake/id_rsa"),
            expected_docker_version="29.6.1", retry_delay=0,
        )

        assert result == []

    def test_docker_version_unparseable_actual_fails_check(self, monkeypatch):
        """Garbled/empty `docker --version` output has no parseable major ->
        the check fails, conservatively, rather than silently passing."""
        monkeypatch.setattr(
            "cspawn.cli.node._ssh_exec",
            _make_ssh_exec(docker_output=""),
        )

        result = _verify_node_provisioning(
            "10.0.0.5", Path("/fake/id_rsa"),
            expected_docker_version="29.6.1", retry_delay=0,
        )

        assert len(result) == 1
        assert "29.6.1" in result[0]

    def test_cloud_init_not_done_reports_actual_status(self, monkeypatch):
        monkeypatch.setattr(
            "cspawn.cli.node._ssh_exec",
            _make_ssh_exec(cloud_init_output="status: running"),
        )

        result = _verify_node_provisioning(
            "10.0.0.5", Path("/fake/id_rsa"),
            expected_docker_version="29.6.1", retry_delay=0,
        )

        assert len(result) == 1
        assert "status: running" in result[0]

    def test_expected_version_none_skips_version_check(self, monkeypatch):
        """expected_docker_version=None: no false failure, even with a 'bad' version string."""
        monkeypatch.setattr(
            "cspawn.cli.node._ssh_exec",
            _make_ssh_exec(docker_output="Docker version 1.0.0, build zzz"),
        )

        result = _verify_node_provisioning(
            "10.0.0.5", Path("/fake/id_rsa"),
            expected_docker_version=None, retry_delay=0,
        )

        assert result == []

    def test_multiple_failures_all_reported(self, monkeypatch):
        """Independent checks all fail -> all three failures are in the list."""
        monkeypatch.setattr(
            "cspawn.cli.node._ssh_exec",
            _make_ssh_exec(
                ssh_connect_results=[False, False, False],
                docker_output="Docker version 1.0.0, build zzz",
                cloud_init_output="status: error",
            ),
        )

        result = _verify_node_provisioning(
            "10.0.0.5", Path("/fake/id_rsa"),
            expected_docker_version="29.6.1", ssh_checks=3, retry_delay=0,
        )

        assert len(result) == 3


# ---------------------------------------------------------------------------
# expand() CLI wiring
# ---------------------------------------------------------------------------

def _invoke_expand(cfg: dict, *, verify_failures=None, manager_docker_version=None):
    """Invoke `expand` (default all-steps flow) with DO/Docker/SSH infra mocked.

    `verify_failures` is the return value of the mocked
    `_verify_node_provisioning` (default: `[]`, i.e. verification passes).

    `manager_docker_version` configures what `manager_client.version()`
    reports (via `_manager_docker_version`): `None` (default) simulates a
    manager whose version can't be determined (`.version()` returns `{}`,
    same as `_manager_docker_version`'s "not found" case), so `expand`'s
    `expected_docker_version` falls through to the `_expected_docker_version`
    file-literal fallback. Pass a string (e.g. "29.7.2") to simulate a
    reachable manager reporting that docker-ce version.

    Returns (result, mocks) so callers can assert on both CLI outcome and
    which collaborators were invoked.
    """
    verify_failures = [] if verify_failures is None else verify_failures

    mock_droplet = MagicMock()
    mock_create_droplet = MagicMock(
        return_value=(mock_droplet, "10.0.0.5", "swarm5.example.com", "swarm5")
    )
    mock_configure_node = MagicMock(return_value=("10.0.0.5", "swarm5"))
    mock_join_swarm = MagicMock(return_value=None)
    mock_verify = MagicMock(return_value=verify_failures)
    mock_ensure_priv_key = MagicMock(return_value=(Path("/fake/id_rsa"), Path("/fake/id_rsa.pub")))
    mock_swarm_node_obj = MagicMock()
    mock_find_swarm_node = MagicMock(return_value=mock_swarm_node_obj)
    mock_drain_swarm_node = MagicMock(return_value=None)
    mock_sync_domains = MagicMock(return_value=None)

    node_mock = MagicMock()
    node_mock.attrs = {"Description": {"Hostname": "swarm5.example.com"}}
    manager_client = MagicMock()
    manager_client.nodes.list.return_value = [node_mock]
    manager_client.version.return_value = (
        {"Version": manager_docker_version} if manager_docker_version else {}
    )

    mock_docker_client_cls = MagicMock(return_value=manager_client)
    mock_do_manager_cls = MagicMock(return_value=MagicMock())

    with (
        patch("cspawn.cli.node.get_config", return_value=cfg),
        patch("cspawn.cli.node.get_logger", return_value=MagicMock()),
        patch("cspawn.cli.node.docker.DockerClient", mock_docker_client_cls),
        patch("cspawn.cli.node.digitalocean.Manager", mock_do_manager_cls),
        patch("cspawn.cli.node._create_droplet", mock_create_droplet),
        patch("cspawn.cli.node._configure_node", mock_configure_node),
        patch("cspawn.cli.node._join_swarm", mock_join_swarm),
        patch("cspawn.cli.node._verify_node_provisioning", mock_verify),
        patch("cspawn.cli.node._ensure_priv_key", mock_ensure_priv_key),
        patch("cspawn.cli.node._find_swarm_node", mock_find_swarm_node),
        patch("cspawn.cli.node._drain_swarm_node", mock_drain_swarm_node),
        patch("cspawn.cli.node._sync_domain_records", mock_sync_domains),
    ):
        runner = CliRunner()
        result = runner.invoke(expand, [])

    mocks = {
        "create_droplet": mock_create_droplet,
        "configure_node": mock_configure_node,
        "join_swarm": mock_join_swarm,
        "verify": mock_verify,
        "ensure_priv_key": mock_ensure_priv_key,
        "find_swarm_node": mock_find_swarm_node,
        "drain_swarm_node": mock_drain_swarm_node,
        "sync_domains": mock_sync_domains,
    }
    return result, mocks


BASE_CFG = {
    "DO_TOKEN": "fake-token",
    "DO_NAMES": "swarm{serial}.example.com",
    "DOCKER_URI": "ssh://fake-manager.example.com",
}


class TestExpandVerificationFailure:
    def test_failure_exits_nonzero_and_drains_node(self):
        """A verification failure aborts the command and attempts a drain."""
        result, mocks = _invoke_expand(BASE_CFG, verify_failures=["docker version mismatch: ..."])

        assert result.exit_code != 0, result.output
        mocks["verify"].assert_called_once()
        mocks["find_swarm_node"].assert_called_once()
        mocks["drain_swarm_node"].assert_called_once()
        assert "Created and joined node" not in result.output

    def test_failure_message_names_the_failures(self):
        result, mocks = _invoke_expand(
            BASE_CFG, verify_failures=["cloud-init not done: status='status: running'"]
        )

        assert result.exit_code != 0
        assert "cloud-init not done" in result.output


class TestExpandVerificationSuccess:
    def test_success_exits_zero_with_unchanged_summary(self):
        """Verification passing leaves the existing summary output unchanged."""
        result, mocks = _invoke_expand(BASE_CFG, verify_failures=[])

        assert result.exit_code == 0, result.output
        assert "Created and joined node: swarm5.example.com" in result.output
        mocks["verify"].assert_called_once()
        mocks["find_swarm_node"].assert_not_called()
        mocks["drain_swarm_node"].assert_not_called()
        mocks["sync_domains"].assert_called_once()

    def test_verify_called_with_resolved_ip_and_key(self):
        result, mocks = _invoke_expand(BASE_CFG, verify_failures=[])

        assert result.exit_code == 0, result.output
        args, kwargs = mocks["verify"].call_args
        assert args[0] == "10.0.0.5"
        assert args[1] == Path("/fake/id_rsa")
        assert kwargs["expected_docker_version"] is None

    def test_verify_uses_manager_docker_version_when_available(self):
        """When the manager's live docker-ce version can be determined,
        expand() passes it as expected_docker_version -- not the (possibly
        stale) file-literal fallback."""
        result, mocks = _invoke_expand(
            BASE_CFG, verify_failures=[], manager_docker_version="29.7.2",
        )

        assert result.exit_code == 0, result.output
        _, kwargs = mocks["verify"].call_args
        assert kwargs["expected_docker_version"] == "29.7.2"

    def test_verify_falls_back_to_expected_docker_version_when_manager_unknown(self):
        """When the manager's live version can't be determined,
        expand() falls back to the _expected_docker_version file-literal
        parse rather than skipping the check outright."""
        with patch("cspawn.cli.node._expected_docker_version", return_value="28.1.0"):
            result, mocks = _invoke_expand(
                BASE_CFG, verify_failures=[], manager_docker_version=None,
            )

        assert result.exit_code == 0, result.output
        _, kwargs = mocks["verify"].call_args
        assert kwargs["expected_docker_version"] == "28.1.0"
