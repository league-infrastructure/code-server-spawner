"""Unit tests for sprint-009 ticket-002: post-join provisioning verification
in `cspawnctl node expand`, and sprint-013 ticket-001: pre-pull codehost
images at node-expand (drain, warm, activate).

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
- `_activate_swarm_node`: idempotent activate mirroring `_drain_swarm_node`,
  with retry+backoff and a loud ERROR on exhausted retries.
- `_get_prepull_images`: DB-derived `class_proto.image_uri` list unioned with
  the optional `NODE_PREPULL_IMAGES` config allowlist.
- `_prepull_images`: best-effort per-image `docker pull` over SSH.
- `_ssh_exec`'s extended `command_timeout` parameter.
- `expand()`'s new drain-before-verify and pre-pull-then-activate wiring.

Follows `test/test_node_cloud_init.py`'s `find_parent_dir`-patch /
`tmp_path`-as-project-root convention and `test/test_node_labels.py`'s
`get_config`/`get_logger` CliRunner mocking convention.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest
from click.testing import CliRunner

from cspawn.cli.node import (
    _activate_swarm_node,
    _expected_docker_version,
    _get_prepull_images,
    _major,
    _manager_docker_version,
    _prepull_images,
    _ssh_exec,
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

def _invoke_expand(cfg: dict, *, verify_failures=None, manager_docker_version=None,
                    call_order: list[str] | None = None, prepull_result=None,
                    prepull_images_list=None):
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

    `call_order`, when given a list, has `"drain"`, `"verify"`, `"prepull"`,
    and `"activate"` appended to it (in call order) by the respective mocks —
    lets callers assert sequencing without depending on Mock internals.

    `prepull_result` configures `_prepull_images`'s return value (default
    `{}`); `prepull_images_list` configures `_get_prepull_images`'s return
    value (default `[]`).

    Returns (result, mocks) so callers can assert on both CLI outcome and
    which collaborators were invoked.
    """
    verify_failures = [] if verify_failures is None else verify_failures
    prepull_result = {} if prepull_result is None else prepull_result
    prepull_images_list = [] if prepull_images_list is None else prepull_images_list
    order: list[str] = [] if call_order is None else call_order

    mock_droplet = MagicMock()
    mock_create_droplet = MagicMock(
        return_value=(mock_droplet, "10.0.0.5", "swarm5.example.com", "swarm5")
    )
    mock_configure_node = MagicMock(return_value=("10.0.0.5", "swarm5"))
    mock_join_swarm = MagicMock(return_value=None)

    def _verify_side_effect(*args, **kwargs):
        order.append("verify")
        return verify_failures

    def _drain_side_effect(*args, **kwargs):
        order.append("drain")

    def _get_images_side_effect(cfg):
        order.append("get_prepull_images")
        return prepull_images_list

    def _prepull_side_effect(*args, **kwargs):
        order.append("prepull")
        return prepull_result

    def _activate_side_effect(*args, **kwargs):
        order.append("activate")
        return True

    mock_verify = MagicMock(side_effect=_verify_side_effect)
    mock_ensure_priv_key = MagicMock(return_value=(Path("/fake/id_rsa"), Path("/fake/id_rsa.pub")))
    mock_swarm_node_obj = MagicMock()
    mock_find_swarm_node = MagicMock(return_value=mock_swarm_node_obj)
    mock_drain_swarm_node = MagicMock(side_effect=_drain_side_effect)
    mock_sync_domains = MagicMock(return_value=None)
    mock_get_prepull_images = MagicMock(side_effect=_get_images_side_effect)
    mock_prepull_images = MagicMock(side_effect=_prepull_side_effect)
    mock_activate_swarm_node = MagicMock(side_effect=_activate_side_effect)
    mock_get_app = MagicMock(return_value=MagicMock())

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
        patch("cspawn.cli.node._get_prepull_images", mock_get_prepull_images),
        patch("cspawn.cli.node._prepull_images", mock_prepull_images),
        patch("cspawn.cli.node._activate_swarm_node", mock_activate_swarm_node),
        patch("cspawn.cli.util.get_app", mock_get_app),
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
        "get_prepull_images": mock_get_prepull_images,
        "prepull_images": mock_prepull_images,
        "activate_swarm_node": mock_activate_swarm_node,
        "get_app": mock_get_app,
        "call_order": order,
    }
    return result, mocks


BASE_CFG = {
    "DO_TOKEN": "fake-token",
    "DO_NAMES": "swarm{serial}.example.com",
    "DOCKER_URI": "ssh://fake-manager.example.com",
}


class TestExpandVerificationFailure:
    def test_failure_exits_nonzero_and_drains_node(self):
        """A verification failure aborts the command and attempts a drain.

        Drain is now attempted twice: once immediately post-join (before
        verify even runs, ticket-013-001) and once more, unchanged, in the
        verify-failure branch itself (a harmless idempotent no-op in
        practice, kept as a fallback for when the early drain failed).
        """
        result, mocks = _invoke_expand(BASE_CFG, verify_failures=["docker version mismatch: ..."])

        assert result.exit_code != 0, result.output
        mocks["verify"].assert_called_once()
        assert mocks["find_swarm_node"].call_count == 2
        assert mocks["drain_swarm_node"].call_count == 2
        assert "Created and joined node" not in result.output
        # Verification failure aborts before pre-pull/activate ever run.
        mocks["get_prepull_images"].assert_not_called()
        mocks["prepull_images"].assert_not_called()
        mocks["activate_swarm_node"].assert_not_called()

    def test_failure_message_names_the_failures(self):
        result, mocks = _invoke_expand(
            BASE_CFG, verify_failures=["cloud-init not done: status='status: running'"]
        )

        assert result.exit_code != 0
        assert "cloud-init not done" in result.output


class TestExpandVerificationSuccess:
    def test_success_exits_zero_with_unchanged_summary(self):
        """Verification passing leaves the existing summary output unchanged.

        Sprint-013 ticket-001: drain now fires in the happy path too
        (immediately post-join, before verify runs) -- it is no longer true
        that a successful expand never touches find/drain. The node is also
        warmed (pre-pull) and reactivated (activate) after verify passes.
        """
        result, mocks = _invoke_expand(BASE_CFG, verify_failures=[])

        assert result.exit_code == 0, result.output
        assert "Created and joined node: swarm5.example.com" in result.output
        mocks["verify"].assert_called_once()
        mocks["find_swarm_node"].assert_called_once()
        mocks["drain_swarm_node"].assert_called_once()
        mocks["sync_domains"].assert_called_once()
        mocks["get_prepull_images"].assert_called_once()
        mocks["prepull_images"].assert_called_once()
        mocks["activate_swarm_node"].assert_called_once()

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


# ---------------------------------------------------------------------------
# expand() sprint-013 ticket-001: drain -> verify -> pre-pull -> activate
# ordering and best-effort behavior
# ---------------------------------------------------------------------------

class TestExpandPrepullOrdering:
    """Ordering guarantees for the new drain -> verify -> pre-pull -> activate
    sequence wired into expand()'s post-join block."""

    def test_drain_before_verify_before_prepull_before_activate(self):
        result, mocks = _invoke_expand(BASE_CFG, verify_failures=[])

        assert result.exit_code == 0, result.output
        assert mocks["call_order"] == [
            "drain", "verify", "get_prepull_images", "prepull", "activate",
        ]

    def test_verify_failure_aborts_before_prepull_and_activate(self):
        """A verification failure raises before pre-pull/activate ever run --
        only drain (early + failure-branch) and verify appear in the order."""
        result, mocks = _invoke_expand(
            BASE_CFG, verify_failures=["docker version mismatch: ..."]
        )

        assert result.exit_code != 0
        assert "prepull" not in mocks["call_order"]
        assert "get_prepull_images" not in mocks["call_order"]
        assert "activate" not in mocks["call_order"]
        assert mocks["call_order"][0] == "drain"
        assert "verify" in mocks["call_order"]

    def test_pull_failure_is_best_effort_and_does_not_block_activation(self):
        """A failed pre-pull for an image is best-effort: activation still
        happens, and ordering (prepull before activate) is unaffected."""
        result, mocks = _invoke_expand(
            BASE_CFG, verify_failures=[],
            prepull_images_list=["ghcr.io/example/code-server:latest"],
            prepull_result={"ghcr.io/example/code-server:latest": False},
        )

        assert result.exit_code == 0, result.output
        mocks["prepull_images"].assert_called_once()
        mocks["activate_swarm_node"].assert_called_once()
        assert mocks["call_order"] == [
            "drain", "verify", "get_prepull_images", "prepull", "activate",
        ]

    def test_prepull_uses_configured_timeout(self):
        cfg = dict(BASE_CFG)
        cfg["NODE_PREPULL_TIMEOUT_S"] = 120
        result, mocks = _invoke_expand(cfg, verify_failures=[])

        assert result.exit_code == 0, result.output
        _, kwargs = mocks["prepull_images"].call_args
        assert kwargs["timeout"] == 120

    def test_prepull_defaults_to_300s_timeout_when_unconfigured(self):
        result, mocks = _invoke_expand(BASE_CFG, verify_failures=[])

        assert result.exit_code == 0, result.output
        _, kwargs = mocks["prepull_images"].call_args
        assert kwargs["timeout"] == 300

    def test_prepull_and_activate_use_the_same_swarm_node_object(self):
        """The node object resolved by the single early `_find_swarm_node`
        call is reused for activate -- no second lookup in the happy path."""
        result, mocks = _invoke_expand(BASE_CFG, verify_failures=[])

        assert result.exit_code == 0, result.output
        mocks["find_swarm_node"].assert_called_once()
        activate_args, _ = mocks["activate_swarm_node"].call_args
        find_return = mocks["find_swarm_node"].return_value
        assert activate_args[1] is find_return


# ---------------------------------------------------------------------------
# _activate_swarm_node
# ---------------------------------------------------------------------------

class _FakeSwarmNode:
    """A minimal stand-in for a docker-py `Node` object with a real `attrs`
    dict (unlike a bare MagicMock, so idempotency/availability checks behave
    like production code, not like an auto-generated Mock attribute)."""

    def __init__(self, availability: str = "drain", node_id: str = "node-id-1",
                 hostname: str = "swarm5.example.com"):
        self.id = node_id
        self.attrs = {
            "Description": {"Hostname": hostname},
            "Spec": {"Availability": availability},
        }

    def update(self, **kwargs):
        # Mirrors the real, installed docker-py's `Node.update(node_spec)`
        # signature (one positional arg, no kwargs) -- any kwarg-style call
        # always raises TypeError in production.
        raise TypeError("update() got an unexpected keyword argument")


class TestActivateSwarmNode:
    """Direct unit coverage for `_activate_swarm_node` (sprint-013
    ticket-001): idempotent activate mirroring `_drain_swarm_node`'s update
    chain, wrapped in a bounded retry-with-backoff loop, with a loud ERROR
    (not WARNING) on exhausted retries.
    """

    def test_already_active_short_circuits_without_update_call(self):
        node_obj = _FakeSwarmNode(availability="active")
        manager_client = MagicMock()

        result = _activate_swarm_node(manager_client, node_obj)

        assert result is True
        manager_client.api.update_node.assert_not_called()

    def test_high_level_update_succeeds_directly(self):
        """An SDK variant where `.update(availability=...)` itself succeeds
        (no TypeError) never falls through to the low-level API."""
        node_obj = _FakeSwarmNode(availability="drain")
        node_obj.update = MagicMock(return_value=True)
        manager_client = MagicMock()

        result = _activate_swarm_node(manager_client, node_obj)

        assert result is True
        node_obj.update.assert_called_once_with(availability="active")
        manager_client.api.update_node.assert_not_called()

    def test_low_level_fallback_used_when_high_level_raises_typeerror(self):
        """Matches the real, installed docker-py's `Node.update(node_spec)`
        signature, which always raises TypeError for the kwarg-style calls
        this helper tries first -- exercising the low-level
        `api.update_node` fallback, exactly like `_drain_swarm_node`."""
        node_obj = _FakeSwarmNode(availability="drain", node_id="node-id-1")
        manager_client = MagicMock()
        manager_client.api.inspect_node.return_value = {
            "Version": {"Index": 7},
            "Spec": {"Availability": "drain", "Role": "worker"},
        }

        result = _activate_swarm_node(manager_client, node_obj)

        assert result is True
        manager_client.api.update_node.assert_called_once_with(
            "node-id-1", 7, {"Availability": "active", "Role": "worker"}
        )

    def test_retries_then_succeeds(self, monkeypatch):
        """A transient failure on the first attempt (the low-level API
        fallback raising, e.g. a connection blip) is retried -- logged as a
        WARNING, not yet an ERROR -- and the second attempt succeeds."""
        monkeypatch.setattr("cspawn.cli.node.time.sleep", lambda s: None)

        node_obj = _FakeSwarmNode(
            availability="drain", node_id="node-id-2", hostname="flaky.example.com",
        )
        manager_client = MagicMock()
        manager_client.api.inspect_node.side_effect = [
            Exception("connection refused"),
            {"Version": {"Index": 3}, "Spec": {"Availability": "drain"}},
        ]
        log = MagicMock()

        result = _activate_swarm_node(
            manager_client, node_obj, retries=3, initial_delay=0.01, log=log
        )

        assert result is True
        assert manager_client.api.inspect_node.call_count == 2
        log.warning.assert_called_once()
        log.error.assert_not_called()

    def test_exhausts_retries_logs_error_and_returns_false(self, monkeypatch):
        """When every attempt fails, `_activate_swarm_node` returns False and
        logs at ERROR (not WARNING) naming the node and the manual remedy --
        a warmed-but-still-drained node is silently-wasted capacity and must
        be loud."""
        monkeypatch.setattr("cspawn.cli.node.time.sleep", lambda s: None)

        node_obj = _FakeSwarmNode(availability="drain", hostname="stuck.example.com")
        manager_client = MagicMock()
        manager_client.api.inspect_node.side_effect = Exception("connection refused")
        log = MagicMock()

        result = _activate_swarm_node(
            manager_client, node_obj, retries=3, initial_delay=0.01, log=log
        )

        assert result is False
        log.error.assert_called_once()
        error_msg = log.error.call_args[0][0]
        assert "stuck.example.com" in error_msg
        assert "docker node update --availability active" in error_msg
        # Two prior attempts logged as WARNING (not yet the final ERROR).
        assert log.warning.call_count == 2

    def test_default_retries_and_delay_match_ticket_spec(self, monkeypatch):
        """Ticket-013-001 specifies retries=3, initial_delay=2.0 as the
        defaults -- assert the signature default, not just behavior."""
        import inspect

        sig = inspect.signature(_activate_swarm_node)
        assert sig.parameters["retries"].default == 3
        assert sig.parameters["initial_delay"].default == 2.0


# ---------------------------------------------------------------------------
# _get_prepull_images
# ---------------------------------------------------------------------------

class TestGetPrepullImages:
    """Direct unit coverage for `_get_prepull_images` (sprint-013
    ticket-001): DB-derived `class_proto.image_uri` UNIONED with the
    optional `NODE_PREPULL_IMAGES` config allowlist -- the DB list is always
    present, config can only add.
    """

    def test_returns_db_images_when_no_config(self, monkeypatch):
        mock_session = MagicMock()
        mock_session.query.return_value.distinct.return_value.all.return_value = [
            ("ghcr.io/example/code-server-python:latest",),
            ("ghcr.io/example/code-server-java:latest",),
        ]
        monkeypatch.setattr("cspawn.models.db.session", mock_session)

        result = _get_prepull_images({})

        assert result == [
            "ghcr.io/example/code-server-python:latest",
            "ghcr.io/example/code-server-java:latest",
        ]

    def test_unions_db_and_configured_images(self, monkeypatch):
        mock_session = MagicMock()
        mock_session.query.return_value.distinct.return_value.all.return_value = [
            ("ghcr.io/example/code-server-python:latest",),
        ]
        monkeypatch.setattr("cspawn.models.db.session", mock_session)

        result = _get_prepull_images({
            "NODE_PREPULL_IMAGES": "ghcr.io/example/extra:latest, ghcr.io/example/extra2:latest",
        })

        assert result == [
            "ghcr.io/example/code-server-python:latest",
            "ghcr.io/example/extra:latest",
            "ghcr.io/example/extra2:latest",
        ]

    def test_configured_images_cannot_narrow_db_derived_coverage(self, monkeypatch):
        """NODE_PREPULL_IMAGES is a strict union, never an override: setting
        it to a single image must not drop the other DB-derived images."""
        mock_session = MagicMock()
        mock_session.query.return_value.distinct.return_value.all.return_value = [
            ("ghcr.io/example/code-server-python:latest",),
            ("ghcr.io/example/code-server-java:latest",),
        ]
        monkeypatch.setattr("cspawn.models.db.session", mock_session)

        result = _get_prepull_images({
            "NODE_PREPULL_IMAGES": "ghcr.io/example/code-server-python:latest",
        })

        assert "ghcr.io/example/code-server-python:latest" in result
        assert "ghcr.io/example/code-server-java:latest" in result
        assert len(result) == 2

    def test_dedupes_image_appearing_in_both_db_and_config(self, monkeypatch):
        mock_session = MagicMock()
        mock_session.query.return_value.distinct.return_value.all.return_value = [
            ("ghcr.io/example/code-server-python:latest",),
        ]
        monkeypatch.setattr("cspawn.models.db.session", mock_session)

        result = _get_prepull_images({
            "NODE_PREPULL_IMAGES": "ghcr.io/example/code-server-python:latest",
        })

        assert result == ["ghcr.io/example/code-server-python:latest"]

    def test_config_accepts_comma_and_whitespace_separators(self, monkeypatch):
        mock_session = MagicMock()
        mock_session.query.return_value.distinct.return_value.all.return_value = []
        monkeypatch.setattr("cspawn.models.db.session", mock_session)

        result = _get_prepull_images({"NODE_PREPULL_IMAGES": "img:a, img:b  img:c,img:d"})

        assert result == ["img:a", "img:b", "img:c", "img:d"]

    def test_db_failure_falls_back_to_configured_allowlist(self):
        """Called with no active Flask app context (as here), the DB query
        raises -- caught, logged as a WARNING, falls back to the configured
        allowlist. Never raises."""
        result = _get_prepull_images({"NODE_PREPULL_IMAGES": "ghcr.io/example/fallback:latest"})

        assert result == ["ghcr.io/example/fallback:latest"]

    def test_db_failure_and_no_config_returns_empty_list(self):
        assert _get_prepull_images({}) == []


# ---------------------------------------------------------------------------
# _prepull_images
# ---------------------------------------------------------------------------

class TestPrepullImagesHelper:
    """Direct unit coverage for `_prepull_images` (sprint-013 ticket-001):
    best-effort per-image `docker pull` over SSH, never raising, never
    aborting the batch over one bad image.
    """

    def test_all_images_pulled_successfully(self, monkeypatch):
        calls = []

        def _fake_ssh_exec(host, username, key_path, cmd, **kwargs):
            calls.append((host, username, key_path, cmd, kwargs))
            return (0, "Pulling ok", "")

        monkeypatch.setattr("cspawn.cli.node._ssh_exec", _fake_ssh_exec)

        result = _prepull_images(
            "10.0.0.5", Path("/fake/id_rsa"), ["img:a", "img:b"], timeout=42.0
        )

        assert result == {"img:a": True, "img:b": True}
        assert len(calls) == 2
        assert calls[0][3] == "docker pull img:a"
        assert calls[0][4]["command_timeout"] == 42.0

    def test_per_image_nonzero_exit_logs_warning_and_continues(self, monkeypatch):
        def _fake_ssh_exec(host, username, key_path, cmd, **kwargs):
            if "bad" in cmd:
                return (1, "", "no such image")
            return (0, "ok", "")

        monkeypatch.setattr("cspawn.cli.node._ssh_exec", _fake_ssh_exec)
        log = MagicMock()

        result = _prepull_images(
            "10.0.0.5", Path("/fake/id_rsa"), ["img:bad", "img:good"], log=log
        )

        assert result == {"img:bad": False, "img:good": True}
        log.warning.assert_called_once()
        assert "img:bad" in log.warning.call_args[0][0]

    def test_per_image_exception_or_timeout_logs_warning_and_continues(self, monkeypatch):
        def _fake_ssh_exec(host, username, key_path, cmd, **kwargs):
            if "wedged" in cmd:
                raise TimeoutError("simulated wedged pull")
            return (0, "ok", "")

        monkeypatch.setattr("cspawn.cli.node._ssh_exec", _fake_ssh_exec)
        log = MagicMock()

        result = _prepull_images(
            "10.0.0.5", Path("/fake/id_rsa"), ["img:wedged", "img:fine"], log=log
        )

        assert result == {"img:wedged": False, "img:fine": True}
        log.warning.assert_called_once()

    def test_empty_image_list_returns_empty_dict_without_ssh(self, monkeypatch):
        mock_ssh = MagicMock()
        monkeypatch.setattr("cspawn.cli.node._ssh_exec", mock_ssh)

        result = _prepull_images("10.0.0.5", Path("/fake/id_rsa"), [])

        assert result == {}
        mock_ssh.assert_not_called()

    def test_default_timeout_matches_ticket_spec(self):
        import inspect

        sig = inspect.signature(_prepull_images)
        assert sig.parameters["timeout"].default == 300.0


# ---------------------------------------------------------------------------
# _ssh_exec: extended command_timeout parameter
# ---------------------------------------------------------------------------

def _build_fake_ssh_client():
    stdin = MagicMock()
    stdout = MagicMock()
    stderr = MagicMock()
    stdout.channel = MagicMock()
    stdout.channel.recv_exit_status.return_value = 0
    stdout.read.return_value = b"output"
    stderr.read.return_value = b""
    client = MagicMock()
    client.exec_command.return_value = (stdin, stdout, stderr)
    return client, stdout


class TestSshExecCommandTimeout:
    """`_ssh_exec`'s new optional `command_timeout` parameter (sprint-013
    ticket-001): `None` (the default) preserves every existing call site's
    behavior exactly; a concrete value applies `.settimeout(...)` to the
    command channel before reading the exit status/output.
    """

    def test_command_timeout_none_does_not_set_channel_timeout(self, monkeypatch):
        client, stdout = _build_fake_ssh_client()
        monkeypatch.setattr("cspawn.cli.node.paramiko.SSHClient", MagicMock(return_value=client))
        monkeypatch.setattr(
            "cspawn.cli.node.paramiko.RSAKey.from_private_key_file",
            MagicMock(return_value=MagicMock()),
        )

        code, out, err = _ssh_exec("10.0.0.5", "root", Path("/fake/id_rsa"), "true")

        assert code == 0
        assert out == "output"
        stdout.channel.settimeout.assert_not_called()

    def test_command_timeout_set_applies_channel_settimeout(self, monkeypatch):
        client, stdout = _build_fake_ssh_client()
        monkeypatch.setattr("cspawn.cli.node.paramiko.SSHClient", MagicMock(return_value=client))
        monkeypatch.setattr(
            "cspawn.cli.node.paramiko.RSAKey.from_private_key_file",
            MagicMock(return_value=MagicMock()),
        )

        code, out, err = _ssh_exec(
            "10.0.0.5", "root", Path("/fake/id_rsa"), "docker pull img:a",
            command_timeout=42.0,
        )

        assert code == 0
        stdout.channel.settimeout.assert_called_once_with(42.0)

    def test_client_closed_even_when_command_timeout_set(self, monkeypatch):
        client, _stdout = _build_fake_ssh_client()
        monkeypatch.setattr("cspawn.cli.node.paramiko.SSHClient", MagicMock(return_value=client))
        monkeypatch.setattr(
            "cspawn.cli.node.paramiko.RSAKey.from_private_key_file",
            MagicMock(return_value=MagicMock()),
        )

        _ssh_exec(
            "10.0.0.5", "root", Path("/fake/id_rsa"), "docker pull img:a",
            command_timeout=5.0,
        )

        client.close.assert_called_once()
