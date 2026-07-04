"""
Unit tests for stale-node resolution in the cs_docker layer:

    cspawn/cs_docker/proc.py::Service.containers        (hardened)
    cspawn/cs_docker/proc.py::Service.node_missing       (new)
    cspawn/cs_docker/proc.py::Service.first_container()  (new)
    cspawn/cs_docker/csmanager.py::CSMService.to_model() (hardened)

No live Docker daemon in any test here — everything is mocked
(`manager.client.nodes.get` / `nodes.list()`, `manager._node_manager()`,
and the raw docker-py Service object's `.tasks()`/`.attrs`). Follows the
in-memory-SQLite + MagicMock pattern established in test/test_stop_host.py.

Run with::

    uv run pytest test/test_node_missing.py -v
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from docker.errors import NotFound

from cspawn.cs_docker.csmanager import CSMService
from cspawn.cs_docker.proc import Service
from cspawn.models import HostState


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------

def _task(task_id, node_id, container_id, *, state="running", desired="running",
          ts="2024-01-01T00:00:00.000000000Z"):
    """Build a raw Swarm task dict shaped like `Service.container_tasks` expects."""
    return {
        "ID": task_id,
        "NodeID": node_id,
        "DesiredState": desired,
        "Status": {
            "State": state,
            "ContainerStatus": {"ContainerID": container_id},
            "Timestamp": ts,
        },
    }


def _make_raw_service(tasks, *, service_id="svc-test", name="cs-test", labels=None):
    """Build a MagicMock standing in for the raw docker-py Service object."""
    raw = MagicMock()
    raw.id = service_id
    raw.tasks.return_value = tasks
    raw.attrs = {
        "Spec": {
            "Name": name,
            "Labels": labels or {},
        }
    }
    return raw


def _make_node(node_id, hostname="swarm2"):
    node = MagicMock()
    node.id = node_id
    node.attrs = {"Description": {"Hostname": hostname}}
    return node


def _make_manager(*, nodes_get_side_effect=None, live_nodes=None, node_manager=None):
    manager = MagicMock()
    if nodes_get_side_effect is not None:
        manager.client.nodes.get.side_effect = nodes_get_side_effect
    manager.client.nodes.list.return_value = live_nodes or []
    if node_manager is not None:
        manager._node_manager.return_value = node_manager
    return manager


def _make_labels(username, **overrides):
    labels = {
        "jtl.codeserver.username": username,
        "jtl.codeserver.password": "pw",
        "jtl.codeserver.public_url": "https://example.com/",
        "jtl.codeserver.start_time": "2024-01-01T00:00:00-08:00",
    }
    labels.update(overrides)
    return labels


# ---------------------------------------------------------------------------
# Service.containers — hardened against docker.errors.NotFound
# ---------------------------------------------------------------------------

class TestServiceContainers:
    def test_skips_stale_node_task_without_raising_and_yields_healthy_one(self, caplog):
        good_node = _make_node("node-good")
        mock_container = MagicMock()
        mock_container.id = "cont-good"
        mock_container.name = "cs-good"

        n_manager = MagicMock()
        n_manager.get.return_value = mock_container

        def nodes_get(node_id):
            if node_id == "node-good":
                return good_node
            raise NotFound(f"node {node_id} not found")

        manager = _make_manager(
            nodes_get_side_effect=nodes_get,
            live_nodes=[good_node],
            node_manager=n_manager,
        )

        tasks = [
            _task("task-good", "node-good", "cont-good", ts="2024-01-01T00:00:00Z"),
            _task("task-stale", "node-stale", "cont-stale", ts="2024-01-01T00:00:01Z"),
        ]
        raw = _make_raw_service(tasks, name="cs-test")
        service = Service(manager, raw)

        with caplog.at_level("ERROR", logger="cspawn.docker"):
            containers = list(service.containers)

        assert len(containers) == 1
        assert containers[0] is mock_container

        error_messages = [
            rec.getMessage() for rec in caplog.records
            if rec.levelname == "ERROR" and rec.name == "cspawn.docker"
        ]
        assert any(
            "node-stale" in msg and "task-stale" in msg and "cs-test" in msg
            for msg in error_messages
        ), error_messages

    def test_all_tasks_stale_yields_nothing_without_raising(self):
        manager = _make_manager(
            nodes_get_side_effect=NotFound("node not found"),
            live_nodes=[],
        )
        tasks = [_task("task-1", "node-dead", "cont-1")]
        raw = _make_raw_service(tasks)
        service = Service(manager, raw)

        containers = list(service.containers)

        assert containers == []


# ---------------------------------------------------------------------------
# Service.node_missing
# ---------------------------------------------------------------------------

class TestNodeMissing:
    def test_true_when_task_node_not_in_nodes_list(self):
        live_node = _make_node("node-live")
        manager = _make_manager(live_nodes=[live_node])
        tasks = [_task("task-1", "node-dead", "cont-1")]
        raw = _make_raw_service(tasks)
        service = Service(manager, raw)

        assert service.node_missing is True

    def test_false_when_task_node_present_in_nodes_list(self):
        live_node = _make_node("node-live")
        manager = _make_manager(live_nodes=[live_node])
        tasks = [_task("task-1", "node-live", "cont-1")]
        raw = _make_raw_service(tasks)
        service = Service(manager, raw)

        assert service.node_missing is False

    def test_false_when_container_tasks_empty(self):
        manager = _make_manager(live_nodes=[_make_node("node-live")])
        raw = _make_raw_service([])
        service = Service(manager, raw)

        assert service.node_missing is False
        # Cheap short-circuit: nodes.list() must not even be called when
        # there are no container-bearing tasks to check.
        manager.client.nodes.list.assert_not_called()

    def test_never_raises_notfound_uses_list_not_get(self):
        """node_missing must use nodes.list() only — never nodes.get() — so
        it can never itself raise docker.errors.NotFound."""
        manager = _make_manager(live_nodes=[])
        manager.client.nodes.get.side_effect = AssertionError(
            "node_missing must not call nodes.get()"
        )
        tasks = [_task("task-1", "node-dead", "cont-1")]
        raw = _make_raw_service(tasks)
        service = Service(manager, raw)

        assert service.node_missing is True
        manager.client.nodes.get.assert_not_called()


# ---------------------------------------------------------------------------
# Service.first_container()
# ---------------------------------------------------------------------------

class TestFirstContainer:
    def test_returns_first_live_container_when_present(self):
        good_node = _make_node("node-good")
        mock_container = MagicMock()
        mock_container.id = "cont-good"

        n_manager = MagicMock()
        n_manager.get.return_value = mock_container

        manager = _make_manager(
            nodes_get_side_effect=lambda node_id: good_node,
            live_nodes=[good_node],
            node_manager=n_manager,
        )
        tasks = [_task("task-1", "node-good", "cont-good")]
        raw = _make_raw_service(tasks)
        service = Service(manager, raw)

        assert service.first_container() is mock_container

    def test_raises_value_error_naming_stale_node_when_node_missing(self):
        manager = _make_manager(
            nodes_get_side_effect=NotFound("gone"),
            live_nodes=[],  # "node-dead" not present -> node_missing True
        )
        tasks = [_task("task-1", "node-dead", "cont-1")]
        raw = _make_raw_service(tasks)
        service = Service(manager, raw)

        with pytest.raises(ValueError, match="node no longer exists"):
            service.first_container()

    def test_raises_generic_value_error_when_no_containers_and_node_present(self):
        manager = _make_manager(live_nodes=[])
        raw = _make_raw_service([])  # no tasks at all -> node_missing False
        service = Service(manager, raw)

        with pytest.raises(ValueError) as excinfo:
            service.first_container()

        assert "No containers found" in str(excinfo.value)
        assert "node no longer exists" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# CSMService.to_model() — MIA-marking vs. preserved-behavior branches
# ---------------------------------------------------------------------------

def _make_flask_app():
    """Create a minimal in-memory Flask app wired to cspawn models."""
    from flask import Flask
    from cspawn.models import db as _db

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-node-missing-secret"
    app.config["TESTING"] = True

    _db.init_app(app)

    with app.app_context():
        _db.create_all()

    return app, _db


_user_counter = 0


def _make_user(db, prefix="nodemissing"):
    from cspawn.models import User

    global _user_counter
    _user_counter += 1
    suffix = _user_counter

    username = f"{prefix}{suffix}"
    user = User(
        user_id=f"uid-{username}",
        email=f"{username}@example.com",
        username=username,
        is_active=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


class TestToModel:
    def test_marks_mia_when_node_missing_true(self, caplog):
        app, db = _make_flask_app()
        with app.app_context():
            user = _make_user(db)

            manager = _make_manager(
                nodes_get_side_effect=NotFound("gone"),
                live_nodes=[],  # "node-dead" not present -> node_missing True
            )
            tasks = [_task("task-1", "node-dead", "cont-1", state="running")]
            raw = _make_raw_service(
                tasks, service_id="svc-mia", labels=_make_labels(user.username)
            )
            service = CSMService(manager, raw)

            # Sanity: Swarm still reports the stale task as "running" — this is
            # exactly the value to_model() must override, not trust.
            assert service.status == "running"

            with caplog.at_level("ERROR", logger="cspawn.docker"):
                m = service.to_model()

            assert m.state == HostState.MIA.value
            assert m.app_state == HostState.MIA.value
            assert m.container_id is None
            assert m.node_id is None

    def test_leaves_state_untouched_when_no_tasks_yet(self):
        """A freshly created service with no task yet must not be flagged MIA,
        and app_state must not appear in the kwargs at all."""
        app, db = _make_flask_app()
        with app.app_context():
            user = _make_user(db)

            manager = _make_manager(live_nodes=[])
            raw = _make_raw_service(
                [], service_id="svc-fresh", labels=_make_labels(user.username)
            )
            service = CSMService(manager, raw)

            m = service.to_model()

            assert m.state == "unknown"  # self.status, unchanged
            assert "app_state" not in m.__dict__
            manager.client.nodes.list.assert_not_called()

    def test_leaves_state_untouched_on_connection_error_transient_blip(self):
        """The pre-existing ConnectionError/OSError branch (node genuinely
        still exists, just transiently unreachable) must not be reclassified
        as MIA, and app_state must not appear in the kwargs at all."""
        app, db = _make_flask_app()
        with app.app_context():
            user = _make_user(db)

            live_node = _make_node("node-1")
            manager = _make_manager(
                nodes_get_side_effect=ConnectionError("boom"),
                live_nodes=[live_node],  # node genuinely still exists
            )
            tasks = [_task("task-1", "node-1", "cont-1", state="running")]
            raw = _make_raw_service(
                tasks, service_id="svc-blip", labels=_make_labels(user.username)
            )
            service = CSMService(manager, raw)

            m = service.to_model()

            assert m.state == "running"  # self.status, unchanged
            assert "app_state" not in m.__dict__

    def test_no_container_flag_never_forces_mia(self):
        """no_container=True (fresh new_cs()) must never trigger the MIA
        branch, even if node_missing would otherwise be True."""
        app, db = _make_flask_app()
        with app.app_context():
            user = _make_user(db)

            manager = _make_manager(
                nodes_get_side_effect=NotFound("gone"),
                live_nodes=[],
            )
            tasks = [_task("task-1", "node-dead", "cont-1")]
            raw = _make_raw_service(
                tasks, service_id="svc-new", labels=_make_labels(user.username)
            )
            service = CSMService(manager, raw)

            # Sanity: node_missing is actually True here — the only thing
            # suppressing the MIA branch must be no_container=True.
            assert service.node_missing is True

            m = service.to_model(no_container=True)

            assert m.state == service.status  # unchanged, not overridden to MIA
            assert m.state != HostState.MIA.value
            assert "app_state" not in m.__dict__


# ---------------------------------------------------------------------------
# CSMService.sync_to_db() — existing row app_state must not be clobbered
# ---------------------------------------------------------------------------

class TestSyncToDbAppStatePreservation:
    def test_existing_row_app_state_untouched_when_not_forcing_mia(self):
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        with app.app_context():
            user = _make_user(db)

            live_node = _make_node("node-1")
            manager = _make_manager(
                nodes_get_side_effect=ConnectionError("boom"),
                live_nodes=[live_node],
            )
            tasks = [_task("task-1", "node-1", "cont-1", state="running")]
            raw = _make_raw_service(
                tasks, service_id="svc-existing", labels=_make_labels(user.username)
            )
            service = CSMService(manager, raw)

            existing = CodeHost(
                user_id=user.id,
                service_id="svc-existing",
                service_name="cs-existing",
                state="running",
                app_state="ready",
            )
            db.session.add(existing)
            db.session.commit()
            host_id = existing.id

            service.sync_to_db()

            refreshed = CodeHost.query.get(host_id)
            assert refreshed.state == "running"
            assert refreshed.app_state == "ready"  # untouched, not clobbered to None

    def test_existing_row_marked_mia_when_node_missing_true(self):
        from cspawn.models import CodeHost

        app, db = _make_flask_app()
        with app.app_context():
            user = _make_user(db)

            manager = _make_manager(
                nodes_get_side_effect=NotFound("gone"),
                live_nodes=[],
            )
            tasks = [_task("task-1", "node-dead", "cont-1", state="running")]
            raw = _make_raw_service(
                tasks, service_id="svc-mia-existing", labels=_make_labels(user.username)
            )
            service = CSMService(manager, raw)

            existing = CodeHost(
                user_id=user.id,
                service_id="svc-mia-existing",
                service_name="cs-mia",
                state="running",
                app_state="ready",
            )
            db.session.add(existing)
            db.session.commit()
            host_id = existing.id

            service.sync_to_db()

            refreshed = CodeHost.query.get(host_id)
            assert refreshed.state == HostState.MIA.value
            assert refreshed.app_state == HostState.MIA.value
