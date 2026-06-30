"""Tests for ticket 006-003: admin /nodes routes.

Covers:
- GET /admin/nodes: 200, node_rows in context, tiers in context.
- POST /admin/nodes/start valid tier: NodeOp created, Popen called, redirect.
- POST /admin/nodes/start invalid tier: no NodeOp, no Popen, flash error, redirect.
- POST /admin/nodes/remove worker node: NodeOp created, Popen called, redirect.
- POST /admin/nodes/remove manager/leader node: refused, no NodeOp, no Popen.
- GET /admin/nodes/op/<id>/status: JSON response with correct fields and log_tail.
- GET /admin/nodes/op/<id>/status unknown id: 404.
- GET /admin/nodes/op/<id>/log: plain-text response; 404 for unknown id.
- Non-admin access to all routes: 302 redirect to main.index.

No live Docker, DigitalOcean, or real subprocess calls in any test.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from flask_login import LoginManager

from cspawn.models import NodeOp, User, db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def flask_app(tmp_path):
    """Minimal Flask test app with in-memory SQLite and admin + main blueprints."""
    cspawn_dir = os.path.join(os.path.dirname(__file__), "..")

    app = Flask(
        __name__,
        template_folder=os.path.join(cspawn_dir, "cspawn", "admin", "templates"),
    )
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SECRET_KEY"] = "test-secret"
    app.config["LOGIN_DISABLED"] = False
    app.config["NODE_TIERS"] = json.dumps([
        {"name": "small", "slug": "s-1vcpu-2gb", "capacity": 4},
        {"name": "large", "slug": "s-4vcpu-8gb", "capacity": 10},
    ])
    app.config["DOCKER_URI"] = "ssh://fake-manager"
    app.config["JTL_DEPLOYMENT"] = "devel"

    from flask_bootstrap import Bootstrap5
    Bootstrap5(app)
    db.init_app(app)

    # Register admin blueprint — has its own template_folder
    from cspawn.admin import admin_bp
    app.register_blueprint(admin_bp, url_prefix="/admin")

    # Register the real main blueprint so that url_for("main.index") works
    # and its template folder (which contains base/page.html) is available.
    from cspawn.main import main_bp
    app.register_blueprint(main_bp)

    # Flask-Login
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "main.index"

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    with app.app_context():
        db.create_all()
        # Attach app_config to the app itself (mirrors cspawn.init behaviour)
        app.app_config = {
            "DOCKER_URI": "ssh://fake-manager",
            "JTL_DEPLOYMENT": "devel",
            "NODE_TIERS": app.config["NODE_TIERS"],
        }
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(flask_app):
    return flask_app.test_client()


def _make_user(flask_app, username: str, is_admin: bool = False) -> int:
    with flask_app.app_context():
        user = User(
            user_id=f"uid-{username}",
            username=username,
            is_admin=is_admin,
            is_active=True,
        )
        db.session.add(user)
        db.session.commit()
        return user.id


def _login(client, flask_app, user_id: int):
    with flask_app.app_context():
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user_id)
            sess["_fresh"] = True


@pytest.fixture()
def admin_user(flask_app):
    return _make_user(flask_app, "admin1", is_admin=True)


@pytest.fixture()
def plain_user(flask_app):
    return _make_user(flask_app, "user1", is_admin=False)


# ---------------------------------------------------------------------------
# Helper: build mock docker node
# ---------------------------------------------------------------------------

def _mock_node(hostname: str, role: str = "worker", is_leader: bool = False,
               tier: str = "small", capacity: str = "4", addr: str = "10.0.0.1"):
    """Return a MagicMock that looks like a docker swarm node."""
    node = MagicMock()
    ms = {}
    if role == "manager" or is_leader:
        ms = {"Leader": is_leader, "Addr": addr + ":2377"}
    node.attrs = {
        "Spec": {
            "Role": role,
            "Availability": "active",
            "Labels": {"cs.tier": tier, "cs.capacity": capacity},
        },
        "Description": {"Hostname": hostname},
        "Status": {"Addr": addr},
        "ManagerStatus": ms if ms else None,
    }
    return node


def _mock_docker_with_nodes(nodes):
    mock_client = MagicMock()
    mock_client.nodes.list.return_value = nodes
    return mock_client


# ---------------------------------------------------------------------------
# GET /admin/nodes
# ---------------------------------------------------------------------------

class TestListNodes:
    """Tests for GET /admin/nodes.

    render_template is mocked to avoid requiring the full Bootstrap/FontAwesome
    extension stack in the test app. The route logic (node_rows assembly,
    tiers, recent_ops) is verified via the mock call args.
    """

    def _get_nodes(self, client, admin_user, flask_app, nodes, host_counts):
        _login(client, flask_app, admin_user)
        with patch("cspawn.admin.routes.docker.DockerClient") as mock_dc, \
             patch("cspawn.cli.node.count_hosts_per_node", return_value=host_counts), \
             patch("cspawn.admin.routes.render_template", return_value="ok") as mock_rt:
            mock_dc.return_value = _mock_docker_with_nodes(nodes)
            resp = client.get("/admin/nodes")
        return resp, mock_rt

    def test_returns_200(self, flask_app, client, admin_user):
        worker = _mock_node("worker1.example.com", role="worker")
        resp, _ = self._get_nodes(client, admin_user, flask_app, [worker], {"worker1": 2})
        assert resp.status_code == 200

    def test_node_rows_passed_to_template(self, flask_app, client, admin_user):
        worker = _mock_node("worker1.example.com", role="worker", addr="10.0.0.2")
        resp, mock_rt = self._get_nodes(client, admin_user, flask_app, [worker], {"worker1": 3})

        assert resp.status_code == 200
        _, kwargs = mock_rt.call_args
        node_rows = kwargs["node_rows"]
        assert len(node_rows) == 1
        assert node_rows[0]["hostname"] == "worker1.example.com"
        assert node_rows[0]["host_count"] == 3
        assert node_rows[0]["ip"] == "10.0.0.2"

    def test_tiers_passed_to_template(self, flask_app, client, admin_user):
        resp, mock_rt = self._get_nodes(client, admin_user, flask_app, [], {})

        _, kwargs = mock_rt.call_args
        tiers = kwargs["tiers"]
        tier_names = {t.name for t in tiers}
        assert "small" in tier_names
        assert "large" in tier_names

    def test_recent_ops_passed_to_template(self, flask_app, client, admin_user):
        with flask_app.app_context():
            op = NodeOp(kind="expand", tier="small", status="done")
            db.session.add(op)
            db.session.commit()
            op_id = op.id

        resp, mock_rt = self._get_nodes(client, admin_user, flask_app, [], {})

        _, kwargs = mock_rt.call_args
        recent_ops = kwargs["recent_ops"]
        assert any(o.id == op_id for o in recent_ops)

    def test_docker_error_flashes_and_returns_200(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)

        with patch("cspawn.admin.routes.docker.DockerClient", side_effect=Exception("conn refused")), \
             patch("cspawn.admin.routes.render_template", return_value="ok"):
            resp = client.get("/admin/nodes")

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /admin/nodes/start
# ---------------------------------------------------------------------------

class TestNodesStart:
    def test_valid_tier_creates_nodeop(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)

        with patch("cspawn.admin.routes.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            resp = client.post("/admin/nodes/start", data={"tier": "small"})

        assert resp.status_code == 302

        with flask_app.app_context():
            ops = NodeOp.query.filter_by(kind="expand", tier="small").all()
            assert len(ops) == 1
            assert ops[0].status == "pending"

    def test_valid_tier_calls_popen_with_correct_args(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)

        with patch("cspawn.admin.routes.subprocess.Popen") as mock_popen, \
             patch("cspawn.admin.routes._cspawnctl_path", return_value="/usr/bin/cspawnctl"):
            mock_popen.return_value = MagicMock()
            client.post("/admin/nodes/start", data={"tier": "large"})

        assert mock_popen.called
        args = mock_popen.call_args[0][0]
        assert args[0] == "/usr/bin/cspawnctl"
        assert args[1] == "-d"
        assert args[2] == "devel"
        assert args[3] == "node"
        assert args[4] == "op-run"
        assert len(args[5]) == 36  # UUID

    def test_valid_tier_redirects_to_list_nodes(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)

        with patch("cspawn.admin.routes.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            resp = client.post("/admin/nodes/start", data={"tier": "small"})

        assert resp.status_code == 302
        assert "/admin/nodes" in resp.headers["Location"]

    def test_invalid_tier_does_not_create_nodeop(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)

        with patch("cspawn.admin.routes.subprocess.Popen") as mock_popen:
            resp = client.post("/admin/nodes/start", data={"tier": "nonexistent-tier"})

        assert resp.status_code == 302
        mock_popen.assert_not_called()

        with flask_app.app_context():
            count = NodeOp.query.filter_by(kind="expand", tier="nonexistent-tier").count()
            assert count == 0

    def test_empty_tier_does_not_create_nodeop(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)

        with patch("cspawn.admin.routes.subprocess.Popen") as mock_popen:
            resp = client.post("/admin/nodes/start", data={"tier": ""})

        mock_popen.assert_not_called()
        assert resp.status_code == 302


# ---------------------------------------------------------------------------
# POST /admin/nodes/remove
# ---------------------------------------------------------------------------

class TestNodesRemove:
    def test_worker_node_creates_nodeop(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)
        worker = _mock_node("worker2.example.com", role="worker")

        with patch("cspawn.admin.routes.docker.DockerClient") as mock_dc, \
             patch("cspawn.admin.routes.subprocess.Popen") as mock_popen:
            mock_dc.return_value = _mock_docker_with_nodes([worker])
            mock_popen.return_value = MagicMock()
            resp = client.post("/admin/nodes/remove", data={"fqdn": "worker2.example.com"})

        assert resp.status_code == 302
        with flask_app.app_context():
            ops = NodeOp.query.filter_by(kind="remove", target_fqdn="worker2.example.com").all()
            assert len(ops) == 1

    def test_worker_node_calls_popen(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)
        worker = _mock_node("worker3.example.com", role="worker")

        with patch("cspawn.admin.routes.docker.DockerClient") as mock_dc, \
             patch("cspawn.admin.routes.subprocess.Popen") as mock_popen, \
             patch("cspawn.admin.routes._cspawnctl_path", return_value="/usr/bin/cspawnctl"):
            mock_dc.return_value = _mock_docker_with_nodes([worker])
            mock_popen.return_value = MagicMock()
            client.post("/admin/nodes/remove", data={"fqdn": "worker3.example.com"})

        assert mock_popen.called
        args = mock_popen.call_args[0][0]
        assert args[4] == "op-run"

    def test_manager_node_is_refused(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)
        manager = _mock_node("manager1.example.com", role="manager", is_leader=False)

        with patch("cspawn.admin.routes.docker.DockerClient") as mock_dc, \
             patch("cspawn.admin.routes.subprocess.Popen") as mock_popen:
            mock_dc.return_value = _mock_docker_with_nodes([manager])
            resp = client.post("/admin/nodes/remove", data={"fqdn": "manager1.example.com"})

        assert resp.status_code == 302
        mock_popen.assert_not_called()
        with flask_app.app_context():
            count = NodeOp.query.filter_by(target_fqdn="manager1.example.com").count()
            assert count == 0

    def test_leader_node_is_refused(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)
        leader = _mock_node("leader1.example.com", role="manager", is_leader=True)

        with patch("cspawn.admin.routes.docker.DockerClient") as mock_dc, \
             patch("cspawn.admin.routes.subprocess.Popen") as mock_popen:
            mock_dc.return_value = _mock_docker_with_nodes([leader])
            resp = client.post("/admin/nodes/remove", data={"fqdn": "leader1.example.com"})

        assert resp.status_code == 302
        mock_popen.assert_not_called()
        with flask_app.app_context():
            count = NodeOp.query.filter_by(target_fqdn="leader1.example.com").count()
            assert count == 0

    def test_empty_fqdn_is_refused(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)

        with patch("cspawn.admin.routes.docker.DockerClient") as mock_dc, \
             patch("cspawn.admin.routes.subprocess.Popen") as mock_popen:
            resp = client.post("/admin/nodes/remove", data={"fqdn": ""})

        mock_popen.assert_not_called()
        assert resp.status_code == 302


# ---------------------------------------------------------------------------
# POST /admin/nodes/rebalance
# ---------------------------------------------------------------------------

class TestNodesRebalance:
    def test_creates_rebalance_nodeop(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)

        with patch("cspawn.admin.routes.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            resp = client.post("/admin/nodes/rebalance")

        assert resp.status_code == 302
        with flask_app.app_context():
            ops = NodeOp.query.filter_by(kind="rebalance").all()
            assert len(ops) == 1
            assert ops[0].status == "pending"

    def test_calls_popen_with_op_run(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)

        with patch("cspawn.admin.routes.subprocess.Popen") as mock_popen, \
             patch("cspawn.admin.routes._cspawnctl_path", return_value="/usr/bin/cspawnctl"):
            mock_popen.return_value = MagicMock()
            client.post("/admin/nodes/rebalance")

        assert mock_popen.called
        args = mock_popen.call_args[0][0]
        assert args[0] == "/usr/bin/cspawnctl"
        assert args[3] == "node"
        assert args[4] == "op-run"
        assert len(args[5]) == 36  # UUID

    def test_redirects_to_list_nodes(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)

        with patch("cspawn.admin.routes.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            resp = client.post("/admin/nodes/rebalance")

        assert "/admin/nodes" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# GET /admin/nodes/op/<op_id>/status
# ---------------------------------------------------------------------------

class TestNodeOpStatus:
    def _make_op(self, flask_app, **kwargs) -> str:
        with flask_app.app_context():
            op = NodeOp(kind="expand", tier="small", status="done", **kwargs)
            db.session.add(op)
            db.session.commit()
            return op.id

    def test_known_op_returns_json(self, flask_app, client, admin_user):
        op_id = self._make_op(flask_app, exit_code=0, message="ok")
        _login(client, flask_app, admin_user)

        resp = client.get(f"/admin/nodes/op/{op_id}/status")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "done"
        assert data["exit_code"] == 0
        assert data["message"] == "ok"
        assert "log_tail" in data

    def test_log_tail_from_file(self, flask_app, client, admin_user, tmp_path):
        log_file = tmp_path / "test-op.log"
        # Write 60 lines; we expect the last 50
        log_file.write_text("\n".join(f"line {i}" for i in range(60)) + "\n")

        op_id = self._make_op(flask_app, log_path=str(log_file))
        _login(client, flask_app, admin_user)

        resp = client.get(f"/admin/nodes/op/{op_id}/status")

        data = resp.get_json()
        tail = data["log_tail"]
        # Must contain line 59 (last) and line 10 (60-50=10), but not line 9\n alone
        assert "line 59" in tail
        assert "line 10" in tail
        # line 9 should not be present (only last 50 of 60 lines → start at line 10)
        assert "line 9\n" not in tail

    def test_log_tail_empty_when_no_file(self, flask_app, client, admin_user):
        op_id = self._make_op(flask_app, log_path="/nonexistent/path.log")
        _login(client, flask_app, admin_user)

        resp = client.get(f"/admin/nodes/op/{op_id}/status")

        data = resp.get_json()
        assert data["log_tail"] == ""

    def test_log_tail_empty_when_log_path_none(self, flask_app, client, admin_user):
        op_id = self._make_op(flask_app)
        _login(client, flask_app, admin_user)

        resp = client.get(f"/admin/nodes/op/{op_id}/status")

        data = resp.get_json()
        assert data["log_tail"] == ""

    def test_unknown_op_returns_404(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)
        resp = client.get("/admin/nodes/op/00000000-0000-0000-0000-000000000000/status")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /admin/nodes/op/<op_id>/log
# ---------------------------------------------------------------------------

class TestNodeOpLog:
    def _make_op(self, flask_app, **kwargs) -> str:
        with flask_app.app_context():
            op = NodeOp(kind="expand", tier="small", status="done", **kwargs)
            db.session.add(op)
            db.session.commit()
            return op.id

    def test_returns_plain_text(self, flask_app, client, admin_user, tmp_path):
        log_file = tmp_path / "full-log.log"
        log_file.write_text("line A\nline B\nline C\n")

        op_id = self._make_op(flask_app, log_path=str(log_file))
        _login(client, flask_app, admin_user)

        resp = client.get(f"/admin/nodes/op/{op_id}/log")

        assert resp.status_code == 200
        assert resp.content_type.startswith("text/plain")
        assert b"line A" in resp.data
        assert b"line C" in resp.data

    def test_empty_when_no_log_file(self, flask_app, client, admin_user):
        op_id = self._make_op(flask_app, log_path="/nonexistent/log.log")
        _login(client, flask_app, admin_user)

        resp = client.get(f"/admin/nodes/op/{op_id}/log")

        assert resp.status_code == 200
        assert resp.data == b""

    def test_unknown_op_returns_404(self, flask_app, client, admin_user):
        _login(client, flask_app, admin_user)
        resp = client.get("/admin/nodes/op/00000000-0000-0000-0000-000000000001/log")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Non-admin access: all routes return 302 redirect
# ---------------------------------------------------------------------------

class TestNonAdminAccess:
    def test_list_nodes_redirects_non_admin(self, flask_app, client, plain_user):
        _login(client, flask_app, plain_user)
        resp = client.get("/admin/nodes")
        assert resp.status_code == 302

    def test_nodes_start_redirects_non_admin(self, flask_app, client, plain_user):
        _login(client, flask_app, plain_user)
        resp = client.post("/admin/nodes/start", data={"tier": "small"})
        assert resp.status_code == 302

    def test_nodes_remove_redirects_non_admin(self, flask_app, client, plain_user):
        _login(client, flask_app, plain_user)
        resp = client.post("/admin/nodes/remove", data={"fqdn": "worker1.example.com"})
        assert resp.status_code == 302

    def test_nodes_rebalance_redirects_non_admin(self, flask_app, client, plain_user):
        _login(client, flask_app, plain_user)
        resp = client.post("/admin/nodes/rebalance")
        assert resp.status_code == 302

    def test_op_status_redirects_non_admin(self, flask_app, client, plain_user):
        _login(client, flask_app, plain_user)
        resp = client.get("/admin/nodes/op/fake-id/status")
        assert resp.status_code == 302

    def test_op_log_redirects_non_admin(self, flask_app, client, plain_user):
        _login(client, flask_app, plain_user)
        resp = client.get("/admin/nodes/op/fake-id/log")
        assert resp.status_code == 302

    def test_list_nodes_redirects_unauthenticated(self, flask_app, client):
        resp = client.get("/admin/nodes")
        assert resp.status_code == 302

    def test_nodes_start_redirects_unauthenticated(self, flask_app, client):
        resp = client.post("/admin/nodes/start", data={"tier": "small"})
        assert resp.status_code == 302
