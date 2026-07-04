"""Unit tests for stale node.hostname pin removal (sprint-008 ticket-003).

Covers:
- `_unpin_services_from_node`: strips only the matching `node.hostname==`
  constraint (FQDN or short-name form), preserves other constraints, leaves
  unrelated/unpinned services untouched, and tolerates per-service failures.
- `graceful_remove_node`: calls `_unpin_services_from_node()` before
  `_drain_swarm_node()`, regardless of whether the swarm node object was
  found.
- `stop_node --force`: attempts the unpin (best-effort) before destroying
  the droplet, and still destroys the droplet even if the unpin attempt
  raises (e.g. Docker unreachable).

All tests use mocked Docker clients — no live Docker/DigitalOcean access.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cspawn.cli.node import (
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
# Tests: stop_node --force best-effort unpin
# ---------------------------------------------------------------------------

class TestStopNodeForceUnpin:
    def _invoke_force(self, unpin_side_effect=None, docker_client_side_effect=None,
                       docker_uri="ssh://fake-manager", destroy_side_effect=None):
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

        with (
            patch("cspawn.cli.node.get_config", return_value=cfg),
            patch("cspawn.cli.node.get_logger", return_value=mock_log),
            patch("cspawn.cli.node.digitalocean.Manager", return_value=MagicMock()),
            patch(
                "cspawn.cli.node._resolve_droplet_by_spec",
                return_value=(droplet, "swarm5.example.com"),
            ),
            patch("cspawn.cli.node.docker.DockerClient", **docker_client_kwargs),
            patch(
                "cspawn.cli.node._unpin_services_from_node",
                side_effect=unpin_side_effect,
                return_value=0,
            ) as mock_unpin,
        ):
            runner = CliRunner(mix_stderr=False)
            result = runner.invoke(
                stop_node,
                ["--force", "swarm5"],
                obj={"v": 0, "deploy": "devel"},
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        return droplet, mock_unpin, mock_log

    def test_force_unpins_before_destroying_droplet(self):
        """--force with DOCKER_URI configured: unpin is attempted before destroy, which still runs."""
        call_order: list[str] = []

        def _record_unpin(*a, **kw):
            call_order.append("unpin")
            return 1

        droplet, mock_unpin, log = self._invoke_force(
            unpin_side_effect=_record_unpin,
            destroy_side_effect=lambda: call_order.append("destroy"),
        )

        assert call_order == ["unpin", "destroy"]
        mock_unpin.assert_called_once()
        droplet.destroy.assert_called_once()

    def test_force_destroys_even_when_docker_unreachable(self):
        """Docker connection failure while building the manager client never blocks destroy()."""
        droplet, mock_unpin, log = self._invoke_force(
            docker_client_side_effect=RuntimeError("connection refused")
        )

        droplet.destroy.assert_called_once()
        log.warning.assert_called_once()
        mock_unpin.assert_not_called()

    def test_force_destroys_even_when_unpin_itself_raises(self):
        """_unpin_services_from_node() raising is caught; destroy still proceeds."""
        droplet, mock_unpin, log = self._invoke_force(
            unpin_side_effect=RuntimeError("docker api error")
        )

        droplet.destroy.assert_called_once()
        log.warning.assert_called_once()
        mock_unpin.assert_called_once()

    def test_force_skips_unpin_when_no_docker_uri_configured(self):
        """Without DOCKER_URI, no DockerClient/unpin attempt is made; destroy still runs."""
        droplet, mock_unpin, log = self._invoke_force(docker_uri=None)

        mock_unpin.assert_not_called()
        droplet.destroy.assert_called_once()
