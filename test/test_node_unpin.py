"""Unit tests for stale node.hostname pin removal (sprint-008 ticket-003).

Covers:
- `_unpin_services_from_node`: strips only the matching `node.hostname==`
  constraint (FQDN or short-name form), preserves other constraints, leaves
  unrelated/unpinned services untouched, tolerates per-service failures, and
  in `dry_run=True` mode counts matches without ever calling `svc.update()`.
- `graceful_remove_node`: on a real run, calls `_unpin_services_from_node()`
  before `_drain_swarm_node()`, regardless of whether the swarm node object
  was found. On a `--dry-run`, it reports how many services would be
  unpinned but makes no mutating call at all.
- `stop_node --force`: attempts the unpin (best-effort) before destroying
  the droplet, still destroys the droplet even if the unpin attempt raises
  (e.g. Docker unreachable), and with `--dry-run` never touches Docker at
  all.
- `_resolve_task_node_fqdn` (sprint-014 ticket-001): resolves the hostname
  once a task carries a `NodeID`; returns `None` (+ WARNING when `log` is
  given) on timeout, whether from no tasks, tasks never getting a `NodeID`,
  or `.tasks()`/`client.nodes.get()` themselves raising -- never propagates.

All tests use mocked Docker clients — no live Docker/DigitalOcean access.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cspawn.cli.node import (
    _resolve_task_node_fqdn,
    _unpin_services_from_node,
    graceful_remove_node,
    stop_node,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service(constraints: list[str], name: str = "svc") -> MagicMock:
    """Build a minimal mock Docker service with the given placement constraints."""
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


def _make_client(services: list) -> MagicMock:
    client = MagicMock()
    client.services.list.return_value = services
    return client


def _make_task(node_id=None):
    """Build a raw Swarm task dict; `NodeID` (a top-level key, a sibling of
    `Status`/`Spec`) is present only when `node_id` is given -- matching a
    task that has been scheduled but has no assigned node yet."""
    task = {"ID": "task-1", "DesiredState": "running", "Status": {"State": "pending"}}
    if node_id is not None:
        task["NodeID"] = node_id
    return task


def _make_raw_service(*, tasks=None, tasks_side_effect=None, service_id="svc-1"):
    """Build a MagicMock standing in for the raw docker-py Service object
    (`CSMService.o`), styled after test_node_missing.py's `_make_raw_service`."""
    raw = MagicMock()
    raw.id = service_id
    if tasks_side_effect is not None:
        raw.tasks.side_effect = tasks_side_effect
    else:
        raw.tasks.return_value = tasks or []
    return raw


def _make_node(hostname="swarm3.example.com"):
    node = MagicMock()
    node.attrs = {"Description": {"Hostname": hostname}}
    return node


# ---------------------------------------------------------------------------
# Tests: _unpin_services_from_node
# ---------------------------------------------------------------------------

class TestUnpinServicesFromNode:
    def test_unpins_fqdn_pin_preserving_other_constraints(self):
        """A service pinned via the full FQDN gets unpinned; other constraints survive."""
        svc = _make_service(["node.role != manager", "node.hostname==swarm5.example.com"])
        client = _make_client([svc])

        count = _unpin_services_from_node(client, "swarm5.example.com")

        assert count == 1
        svc.update.assert_called_once_with(constraints=["node.role != manager"])

    def test_unpins_short_name_pin(self):
        """A service pinned via the short hostname form also gets unpinned."""
        svc = _make_service(["node.hostname==swarm5"])
        client = _make_client([svc])

        count = _unpin_services_from_node(client, "swarm5.example.com")

        assert count == 1
        svc.update.assert_called_once_with(constraints=[])

    def test_matches_pin_with_surrounding_whitespace(self):
        """A constraint written with spaces (e.g. 'node.hostname == x') still matches."""
        svc = _make_service(["node.hostname == swarm5.example.com", "node.role != manager"])
        client = _make_client([svc])

        count = _unpin_services_from_node(client, "swarm5.example.com")

        assert count == 1
        svc.update.assert_called_once_with(constraints=["node.role != manager"])

    def test_leaves_unpinned_service_untouched(self):
        """A service with no node.hostname constraint is never updated."""
        svc = _make_service(["node.role != manager"])
        client = _make_client([svc])

        count = _unpin_services_from_node(client, "swarm5.example.com")

        assert count == 0
        svc.update.assert_not_called()

    def test_leaves_differently_pinned_service_untouched(self):
        """A service pinned to a different node is never updated."""
        svc = _make_service(["node.hostname==swarm9.example.com"])
        client = _make_client([svc])

        count = _unpin_services_from_node(client, "swarm5.example.com")

        assert count == 0
        svc.update.assert_not_called()

    def test_mixed_services_only_matching_one_is_updated(self):
        """Among several services, only the one pinned to the target node is touched."""
        svc_pinned = _make_service(["node.hostname==swarm5.example.com"], name="pinned")
        svc_other_pin = _make_service(["node.hostname==swarm9.example.com"], name="other-pin")
        svc_no_pin = _make_service([], name="no-pin")
        client = _make_client([svc_pinned, svc_other_pin, svc_no_pin])

        count = _unpin_services_from_node(client, "swarm5.example.com")

        assert count == 1
        svc_pinned.update.assert_called_once()
        svc_other_pin.update.assert_not_called()
        svc_no_pin.update.assert_not_called()

    def test_failure_on_one_service_does_not_block_others(self):
        """svc.update() failing for one service is logged, not raised; others still unpinned."""
        svc_fails = _make_service(["node.hostname==swarm5.example.com"], name="fails")
        svc_fails.update.side_effect = RuntimeError("boom")
        svc_ok = _make_service(["node.hostname==swarm5"], name="ok")
        client = _make_client([svc_fails, svc_ok])
        log = MagicMock()

        count = _unpin_services_from_node(client, "swarm5.example.com", log=log)

        # Only the successful update is counted.
        assert count == 1
        svc_fails.update.assert_called_once()
        svc_ok.update.assert_called_once()
        log.warning.assert_called_once()

    def test_no_log_provided_does_not_raise_on_failure(self):
        """log=None (the default) must not itself raise when a service update fails."""
        svc = _make_service(["node.hostname==swarm5.example.com"])
        svc.update.side_effect = RuntimeError("boom")
        client = _make_client([svc])

        count = _unpin_services_from_node(client, "swarm5.example.com")

        assert count == 0

    def test_lists_services_filtered_by_codeserver_label(self):
        """The service listing call is scoped to jtl.codeserver=true services."""
        client = _make_client([])
        _unpin_services_from_node(client, "swarm5.example.com")
        client.services.list.assert_called_once_with(filters={"label": "jtl.codeserver=true"})

    def test_dry_run_counts_matches_without_calling_update(self):
        """dry_run=True reports the count but never mutates the service."""
        svc = _make_service(["node.hostname==swarm5.example.com", "node.role != manager"])
        client = _make_client([svc])

        count = _unpin_services_from_node(client, "swarm5.example.com", dry_run=True)

        assert count == 1
        svc.update.assert_not_called()

    def test_dry_run_zero_when_no_matching_services(self):
        """dry_run=True with no matching pins returns 0 and touches nothing."""
        svc = _make_service(["node.role != manager"])
        client = _make_client([svc])

        count = _unpin_services_from_node(client, "swarm5.example.com", dry_run=True)

        assert count == 0
        svc.update.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: graceful_remove_node ordering
# ---------------------------------------------------------------------------

class TestGracefulRemoveNodeUnpinOrdering:
    def _run(self, node_obj):
        """Invoke graceful_remove_node with everything but unpin/drain mocked."""
        ctx = MagicMock()
        manager_client = MagicMock()
        mgr = MagicMock()
        droplet = MagicMock()
        log = MagicMock()

        call_order: list[str] = []

        def _record_unpin(*a, **kw):
            call_order.append("unpin")
            return 0

        def _record_drain(*a, **kw):
            call_order.append("drain")

        with (
            patch("cspawn.cli.node.get_config", return_value={}),
            patch(
                "cspawn.cli.node._resolve_droplet_by_spec",
                return_value=(droplet, "swarm5.example.com"),
            ),
            patch("cspawn.cli.node._find_swarm_node", return_value=node_obj),
            patch(
                "cspawn.cli.node._unpin_services_from_node",
                side_effect=_record_unpin,
            ) as mock_unpin,
            patch(
                "cspawn.cli.node._drain_swarm_node", side_effect=_record_drain
            ) as mock_drain,
            patch("cspawn.cli.node._wait_node_tasks_drained"),
        ):
            graceful_remove_node(
                ctx, manager_client, mgr, "swarm5.example.com", dry_run=False, log=log
            )

        return call_order, mock_unpin, mock_drain, droplet, manager_client, log

    def test_unpin_called_before_drain_when_node_found(self):
        """When the swarm node object is found, unpin runs before drain."""
        node_obj = MagicMock()
        call_order, mock_unpin, mock_drain, droplet, manager_client, log = self._run(node_obj)

        assert call_order == ["unpin", "drain"]
        mock_unpin.assert_called_once_with(manager_client, "swarm5.example.com", log=log)
        droplet.destroy.assert_called_once()

    def test_unpin_called_even_when_node_not_found(self):
        """A stale pin is meaningful even if the swarm node object is already gone."""
        call_order, mock_unpin, mock_drain, droplet, manager_client, log = self._run(node_obj=None)

        assert call_order == ["unpin"]
        mock_unpin.assert_called_once_with(manager_client, "swarm5.example.com", log=log)
        mock_drain.assert_not_called()
        droplet.destroy.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: graceful_remove_node dry-run must not mutate
# ---------------------------------------------------------------------------

class TestGracefulRemoveNodeDryRun:
    def _run_dry(self, node_obj, services):
        """Invoke graceful_remove_node(dry_run=True) against a real (unmocked)
        `_unpin_services_from_node`, backed by a fake Docker services list."""
        ctx = MagicMock()
        manager_client = MagicMock()
        manager_client.services.list.return_value = services
        mgr = MagicMock()
        droplet = MagicMock()
        droplet.id = "droplet-id"
        log = MagicMock()

        with (
            patch("cspawn.cli.node.get_config", return_value={}),
            patch(
                "cspawn.cli.node._resolve_droplet_by_spec",
                return_value=(droplet, "swarm5.example.com"),
            ),
            patch("cspawn.cli.node._find_swarm_node", return_value=node_obj),
            patch("cspawn.cli.node._drain_swarm_node") as mock_drain,
            patch("cspawn.cli.node._wait_node_tasks_drained") as mock_wait,
        ):
            graceful_remove_node(
                ctx, manager_client, mgr, "swarm5.example.com", dry_run=True, log=log
            )

        return droplet, mock_drain, mock_wait

    def test_dry_run_reports_would_unpin_and_makes_no_mutating_call(self, capsys):
        """--dry-run reports the pinned-service count but never calls svc.update()."""
        svc = _make_service(["node.hostname==swarm5.example.com"])
        node_obj = MagicMock()

        droplet, mock_drain, mock_wait = self._run_dry(node_obj, [svc])

        out = capsys.readouterr().out
        assert "unpin 1 service(s) pinned to swarm5.example.com" in out
        svc.update.assert_not_called()
        mock_drain.assert_not_called()
        mock_wait.assert_not_called()
        droplet.destroy.assert_not_called()

    def test_dry_run_with_no_pinned_services_omits_unpin_line(self, capsys):
        """--dry-run with nothing pinned doesn't mention unpinning at all."""
        svc = _make_service(["node.role != manager"])
        node_obj = MagicMock()

        droplet, mock_drain, mock_wait = self._run_dry(node_obj, [svc])

        out = capsys.readouterr().out
        assert "unpin" not in out
        svc.update.assert_not_called()
        droplet.destroy.assert_not_called()

    def test_dry_run_reports_unpin_even_when_node_not_found(self, capsys):
        """A stale pin is still worth reporting in --dry-run even if the node is already gone."""
        svc = _make_service(["node.hostname==swarm5"])

        droplet, mock_drain, mock_wait = self._run_dry(node_obj=None, services=[svc])

        out = capsys.readouterr().out
        assert "unpin 1 service(s) pinned to swarm5.example.com" in out
        svc.update.assert_not_called()
        droplet.destroy.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: stop_node --force best-effort unpin
# ---------------------------------------------------------------------------

class TestStopNodeForceUnpin:
    def _invoke_force(self, unpin_side_effect=None, docker_client_side_effect=None,
                       docker_uri="ssh://fake-manager", destroy_side_effect=None,
                       dry_run=False):
        cfg = {
            "DO_TOKEN": "fake-token",
            "DO_NAMES": "swarm{serial}.example.com",
            "DO_TAG": "cspawn",
            "DOCKER_URI": docker_uri,
        }
        droplet = MagicMock()
        droplet.id = "droplet-id"
        if destroy_side_effect is not None:
            droplet.destroy.side_effect = destroy_side_effect
        mock_docker_client = MagicMock()
        mock_log = MagicMock()

        docker_client_kwargs = (
            {"side_effect": docker_client_side_effect}
            if docker_client_side_effect is not None
            else {"return_value": mock_docker_client}
        )

        args = ["--force", "--dry-run", "swarm5"] if dry_run else ["--force", "swarm5"]

        with (
            patch("cspawn.cli.node.get_config", return_value=cfg),
            patch("cspawn.cli.node.get_logger", return_value=mock_log),
            patch("cspawn.cli.node.digitalocean.Manager", return_value=MagicMock()),
            patch(
                "cspawn.cli.node._resolve_droplet_by_spec",
                return_value=(droplet, "swarm5.example.com"),
            ),
            patch("cspawn.cli.node.docker.DockerClient", **docker_client_kwargs) as mock_docker_cls,
            patch(
                "cspawn.cli.node._unpin_services_from_node",
                side_effect=unpin_side_effect,
                return_value=0,
            ) as mock_unpin,
        ):
            runner = CliRunner(mix_stderr=False)
            result = runner.invoke(
                stop_node,
                args,
                obj={"v": 0, "deploy": "devel"},
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        return droplet, mock_unpin, mock_log, mock_docker_cls

    def test_force_unpins_before_destroying_droplet(self):
        """--force with DOCKER_URI configured: unpin is attempted before destroy, which still runs."""
        call_order: list[str] = []

        def _record_unpin(*a, **kw):
            call_order.append("unpin")
            return 1

        droplet, mock_unpin, log, _ = self._invoke_force(
            unpin_side_effect=_record_unpin,
            destroy_side_effect=lambda: call_order.append("destroy"),
        )

        assert call_order == ["unpin", "destroy"]
        mock_unpin.assert_called_once()
        droplet.destroy.assert_called_once()

    def test_force_destroys_even_when_docker_unreachable(self):
        """Docker connection failure while building the manager client never blocks destroy()."""
        droplet, mock_unpin, log, _ = self._invoke_force(
            docker_client_side_effect=RuntimeError("connection refused")
        )

        droplet.destroy.assert_called_once()
        log.warning.assert_called_once()
        mock_unpin.assert_not_called()

    def test_force_destroys_even_when_unpin_itself_raises(self):
        """_unpin_services_from_node() raising is caught; destroy still proceeds."""
        droplet, mock_unpin, log, _ = self._invoke_force(
            unpin_side_effect=RuntimeError("docker api error")
        )

        droplet.destroy.assert_called_once()
        log.warning.assert_called_once()
        mock_unpin.assert_called_once()

    def test_force_skips_unpin_when_no_docker_uri_configured(self):
        """Without DOCKER_URI, no DockerClient/unpin attempt is made; destroy still runs."""
        droplet, mock_unpin, log, _ = self._invoke_force(docker_uri=None)

        mock_unpin.assert_not_called()
        droplet.destroy.assert_called_once()

    def test_force_dry_run_never_touches_docker_or_unpin(self):
        """--force --dry-run must not construct a manager client, attempt an unpin,
        or destroy the droplet — dry-run must not mutate cluster state at all."""
        droplet, mock_unpin, log, mock_docker_cls = self._invoke_force(dry_run=True)

        mock_docker_cls.assert_not_called()
        mock_unpin.assert_not_called()
        droplet.destroy.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: _resolve_task_node_fqdn (sprint 014, ticket 001)
# ---------------------------------------------------------------------------

class TestResolveTaskNodeFqdn:
    def test_resolves_once_a_task_carries_a_node_id(self):
        """A task with no NodeID yet is skipped; once one appears, its node's
        hostname is resolved and returned -- no WARNING on the happy path."""
        client = MagicMock()
        client.nodes.get.return_value = _make_node("swarm3.example.com")
        raw = _make_raw_service(
            tasks_side_effect=[[_make_task(None)], [_make_task("node-abc")]]
        )
        log = MagicMock()

        result = _resolve_task_node_fqdn(client, raw, timeout=1.0, poll_interval=0.01, log=log)

        assert result == "swarm3.example.com"
        client.nodes.get.assert_called_once_with("node-abc")
        log.warning.assert_not_called()

    def test_returns_none_and_warns_on_timeout(self):
        """No task is ever assigned a NodeID within `timeout` -> None + WARNING."""
        raw = _make_raw_service(tasks=[_make_task(None)], service_id="svc-timeout")
        client = MagicMock()
        log = MagicMock()

        result = _resolve_task_node_fqdn(client, raw, timeout=0.05, poll_interval=0.01, log=log)

        assert result is None
        client.nodes.get.assert_not_called()
        log.warning.assert_called_once()

    def test_timeout_with_no_tasks_at_all(self):
        """A brand-new service with no tasks yet also degrades to None + WARNING."""
        raw = _make_raw_service(tasks=[], service_id="svc-no-tasks")
        client = MagicMock()
        log = MagicMock()

        result = _resolve_task_node_fqdn(client, raw, timeout=0.05, poll_interval=0.01, log=log)

        assert result is None
        log.warning.assert_called_once()

    def test_never_raises_when_nodes_get_itself_raises(self):
        """A task gets a NodeID, but resolving it via client.nodes.get() raises --
        the helper must swallow it, return None, and log a WARNING, not propagate."""
        raw = _make_raw_service(tasks=[_make_task("node-abc")], service_id="svc-badnode")
        client = MagicMock()
        client.nodes.get.side_effect = RuntimeError("node vanished")
        log = MagicMock()

        result = _resolve_task_node_fqdn(client, raw, timeout=1.0, poll_interval=0.01, log=log)

        assert result is None
        log.warning.assert_called_once()

    def test_never_raises_when_tasks_call_itself_raises(self):
        """service.tasks() raising (e.g. a 404 on a since-removed service)
        degrades to a timeout, not a propagated exception."""
        raw = _make_raw_service(tasks_side_effect=RuntimeError("swarm unreachable"), service_id="svc-err")
        client = MagicMock()
        log = MagicMock()

        result = _resolve_task_node_fqdn(client, raw, timeout=0.03, poll_interval=0.01, log=log)

        assert result is None
        log.warning.assert_called()

    def test_no_log_provided_does_not_raise_on_timeout(self):
        """log=None (the default) must not itself raise -- matches this module's
        existing log=None-means-silent convention."""
        raw = _make_raw_service(tasks=[], service_id="svc-quiet")
        client = MagicMock()

        result = _resolve_task_node_fqdn(client, raw, timeout=0.02, poll_interval=0.01)

        assert result is None

    def test_polls_multiple_times_before_node_id_appears(self):
        """Confirms the bounded poll loop actually re-checks .tasks() rather
        than giving up after a single failed look, as long as time remains."""
        client = MagicMock()
        client.nodes.get.return_value = _make_node("swarm9.example.com")
        raw = _make_raw_service(
            tasks_side_effect=[[_make_task(None)], [_make_task(None)], [_make_task("node-z")]]
        )

        result = _resolve_task_node_fqdn(client, raw, timeout=1.0, poll_interval=0.01)

        assert result == "swarm9.example.com"
        assert raw.tasks.call_count == 3
