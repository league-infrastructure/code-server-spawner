"""Tests for ticket 006-004: admin nodes.html template rendering.

Covers the template-level acceptance criteria:
- GET /admin/nodes renders 200 with correct HTML structure.
- Start buttons render one per tier (not a dropdown select).
- Node table includes column headers: Hostname, IP, Role, Actions.
- Manager/leader rows show no Remove button; worker rows do.
- Remove button includes a JS confirm() call.
- Operations panel section is present.
- op-status-<id> and op-log-<id> elements are present for ops.
- Full log link appears in op panel rows.
- pollOp JS function is emitted for pending/running ops.
- 'interrupted' NodeOps render a distinct badge class (bg-dark), surface
  their message via the badge's title attribute, and are never polled
  (ticket 010-005).
- Non-admin access redirected.
- Empty node_rows (Docker unreachable) still renders 200.
- Nodes nav link appears in the subnav.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from flask import Blueprint, Flask
from flask_bootstrap import Bootstrap5
from flask_font_awesome import FontAwesome
from flask_login import LoginManager

from cspawn.models import NodeOp, User, db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_auth_stub():
    """Minimal auth blueprint so url_for('auth.profile') etc. resolve."""
    auth_stub = Blueprint("auth", __name__)

    @auth_stub.route("/auth/profile")
    def profile():
        return "profile"

    @auth_stub.route("/auth/logout")
    def logout():
        return "logout"

    @auth_stub.route("/auth/login")
    def login():
        return "login"

    return auth_stub


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def flask_app(tmp_path):
    """Flask test app with Bootstrap5 + FontAwesome so the full template chain renders."""
    cspawn_dir = os.path.join(os.path.dirname(__file__), "..")

    app = Flask(
        "cspawn",
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
    app.config["DEFAULT_CAPACITY"] = "6"

    db.init_app(app)

    Bootstrap5(app)
    FontAwesome(app)

    # Auth stub — satisfies url_for('auth.profile') in the base template navbar
    app.register_blueprint(_make_auth_stub(), url_prefix="/auth")

    # Real admin blueprint — routes and templates we're testing
    from cspawn.admin import admin_bp
    app.register_blueprint(admin_bp, url_prefix="/admin")

    # Real main blueprint — provides base/page.html template + main routes
    from cspawn.main import main_bp
    app.register_blueprint(main_bp)

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "main.index"

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    with app.app_context():
        db.create_all()
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
    return _make_user(flask_app, "admin-tmpl", is_admin=True)


@pytest.fixture()
def plain_user(flask_app):
    return _make_user(flask_app, "plain-tmpl", is_admin=False)


def _get_nodes(client, flask_app, admin_user, extra_nodes=None, host_counts=None):
    """GET /admin/nodes with mocked Docker (no live connection)."""
    _login(client, flask_app, admin_user)
    nodes = extra_nodes or []
    counts = host_counts or {}
    with patch("cspawn.admin.routes.docker.DockerClient") as mock_dc, \
         patch("cspawn.cli.node.count_hosts_per_node", return_value=counts):
        mock_dc.return_value.nodes.list.return_value = nodes
        resp = client.get("/admin/nodes")
    return resp


# ---------------------------------------------------------------------------
# Template rendering tests
# ---------------------------------------------------------------------------

class TestNodesTemplateRendering:
    def test_page_returns_200(self, flask_app, client, admin_user):
        resp = _get_nodes(client, flask_app, admin_user)
        assert resp.status_code == 200

    def test_start_buttons_one_per_tier(self, flask_app, client, admin_user):
        """One submit button per tier (not a dropdown)."""
        resp = _get_nodes(client, flask_app, admin_user)
        html = resp.data.decode()
        assert "Start small node" in html
        assert "Start large node" in html
        # Hidden inputs for tier value, not a <select>
        assert 'name="tier"' in html

    def test_node_table_headers_present(self, flask_app, client, admin_user):
        resp = _get_nodes(client, flask_app, admin_user)
        html = resp.data.decode()
        for header in ("Hostname", "IP", "Role", "Tier", "Capacity", "Hosts", "Availability", "Actions"):
            assert header in html, f"Column header '{header}' not found in rendered HTML"

    def test_operations_panel_present(self, flask_app, client, admin_user):
        resp = _get_nodes(client, flask_app, admin_user)
        html = resp.data.decode()
        assert "Operations" in html

    def test_empty_node_rows_renders_without_error(self, flask_app, client, admin_user):
        """Template renders correctly when Docker is unreachable (node_rows=[])."""
        _login(client, flask_app, admin_user)
        with patch("cspawn.admin.routes.docker.DockerClient", side_effect=Exception("conn refused")):
            resp = client.get("/admin/nodes")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "No nodes found" in html

    def test_nodes_nav_link_present(self, flask_app, client, admin_user):
        """'Nodes' nav entry appears in the subnav."""
        resp = _get_nodes(client, flask_app, admin_user)
        html = resp.data.decode()
        assert ">Nodes<" in html

    def test_worker_row_has_remove_button(self, flask_app, client, admin_user):
        """Worker nodes show a Remove button."""
        worker = MagicMock()
        worker.attrs = {
            "Spec": {"Role": "worker", "Availability": "active",
                     "Labels": {"cs.tier": "small", "cs.capacity": "4"}},
            "Description": {"Hostname": "worker1.example.com"},
            "Status": {"Addr": "10.0.0.5"},
            "ManagerStatus": None,
        }
        resp = _get_nodes(client, flask_app, admin_user, extra_nodes=[worker],
                          host_counts={"worker1": 1})
        html = resp.data.decode()
        assert "Remove" in html
        assert "worker1.example.com" in html

    def test_manager_row_has_no_remove_button(self, flask_app, client, admin_user):
        """Manager/leader nodes do not show a Remove button."""
        manager = MagicMock()
        manager.attrs = {
            "Spec": {"Role": "manager", "Availability": "active", "Labels": {}},
            "Description": {"Hostname": "manager1.example.com"},
            "Status": {"Addr": "10.0.0.1"},
            "ManagerStatus": {"Leader": True, "Addr": "10.0.0.1:2377"},
        }
        resp = _get_nodes(client, flask_app, admin_user, extra_nodes=[manager])
        html = resp.data.decode()
        assert "manager1.example.com" in html
        # em-dash placeholder should be present in the Actions cell
        assert "—" in html
        # No Remove button for manager rows
        assert "Remove" not in html

    def test_remove_button_has_confirm_dialog(self, flask_app, client, admin_user):
        """Worker Remove button includes onsubmit confirm()."""
        worker = MagicMock()
        worker.attrs = {
            "Spec": {"Role": "worker", "Availability": "active",
                     "Labels": {"cs.tier": "small", "cs.capacity": "4"}},
            "Description": {"Hostname": "worker99.example.com"},
            "Status": {"Addr": "10.0.0.9"},
            "ManagerStatus": None,
        }
        resp = _get_nodes(client, flask_app, admin_user, extra_nodes=[worker])
        html = resp.data.decode()
        assert "confirm(" in html

    def test_rebalance_button_present(self, flask_app, client, admin_user):
        """The Rebalance Hosts button posts to the rebalance route with a confirm()."""
        resp = _get_nodes(client, flask_app, admin_user)
        html = resp.data.decode()
        assert "/admin/nodes/rebalance" in html
        assert "Rebalance Hosts" in html
        assert "confirm(" in html

    def test_ops_panel_shows_op_status_element(self, flask_app, client, admin_user):
        """op-status-<id> span and op-log-<id> pre are present for ops."""
        with flask_app.app_context():
            op = NodeOp(kind="expand", tier="small", status="running")
            db.session.add(op)
            db.session.commit()
            op_id = op.id

        resp = _get_nodes(client, flask_app, admin_user)
        html = resp.data.decode()
        assert f"op-status-{op_id}" in html
        assert f"op-log-{op_id}" in html

    def test_ops_panel_has_full_log_link(self, flask_app, client, admin_user):
        """Full log link is rendered for each op."""
        with flask_app.app_context():
            op = NodeOp(kind="remove", target_fqdn="worker5.example.com", status="done")
            db.session.add(op)
            db.session.commit()
            op_id = op.id

        resp = _get_nodes(client, flask_app, admin_user)
        html = resp.data.decode()
        assert f"/admin/nodes/op/{op_id}/log" in html
        assert "Full log" in html

    def test_polled_ops_emit_pollop_js(self, flask_app, client, admin_user):
        """pollOp() is called for pending/running ops but not for done ops."""
        with flask_app.app_context():
            op_running = NodeOp(kind="expand", tier="large", status="running")
            op_done = NodeOp(kind="expand", tier="small", status="done")
            db.session.add_all([op_running, op_done])
            db.session.commit()
            running_id = op_running.id
            done_id = op_done.id

        resp = _get_nodes(client, flask_app, admin_user)
        html = resp.data.decode()
        assert f'pollOp("{running_id}")' in html
        assert f'pollOp("{done_id}")' not in html

    def test_interrupted_op_renders_distinct_badge_class(self, flask_app, client, admin_user):
        """An 'interrupted' NodeOp renders with its own badge class (bg-dark),
        distinct from bg-success/bg-danger/bg-warning/bg-secondary."""
        with flask_app.app_context():
            op = NodeOp(kind="expand", tier="large", status="interrupted",
                        message="spawner restarted while op was in flight")
            db.session.add(op)
            db.session.commit()
            op_id = op.id

        resp = _get_nodes(client, flask_app, admin_user)
        html = resp.data.decode()
        assert f'id="op-status-{op_id}"' in html
        # Isolate the badge span for this op and confirm it carries bg-dark,
        # not one of the other status colors.
        needle = f'id="op-status-{op_id}"'
        idx = html.index(needle)
        span_start = html.rfind("<span", 0, idx)
        span_end = html.index("</span>", idx)
        span_html = html[span_start:span_end]
        assert "bg-dark" in span_html
        assert "bg-success" not in span_html
        assert "bg-danger" not in span_html
        assert "bg-warning" not in span_html
        assert "bg-secondary" not in span_html

    def test_interrupted_op_message_visible_via_title_attribute(self, flask_app, client, admin_user):
        """An 'interrupted' op's message (e.g. naming an orphaned droplet) is
        rendered in the page output via the badge's title attribute."""
        with flask_app.app_context():
            op = NodeOp(
                kind="expand", tier="large", status="interrupted",
                message="spawner restarted while op was in flight; droplet "
                         "swarm4.dojtl.net (id=999) may be orphaned — verify it joined the swarm",
            )
            db.session.add(op)
            db.session.commit()

        resp = _get_nodes(client, flask_app, admin_user)
        html = resp.data.decode()
        assert "swarm4.dojtl.net" in html
        assert "may be orphaned" in html

    def test_interrupted_op_does_not_emit_pollop_js(self, flask_app, client, admin_user):
        """Regression guard: 'interrupted' ops must never be polled — they are
        terminal, not in-flight. A 'running' op in the same response is still
        polled, for contrast."""
        with flask_app.app_context():
            op_interrupted = NodeOp(kind="expand", tier="large", status="interrupted",
                                     message="spawner restarted while op was in flight")
            op_running = NodeOp(kind="expand", tier="small", status="running")
            db.session.add_all([op_interrupted, op_running])
            db.session.commit()
            interrupted_id = op_interrupted.id
            running_id = op_running.id

        resp = _get_nodes(client, flask_app, admin_user)
        html = resp.data.decode()
        assert f'pollOp("{interrupted_id}")' not in html
        assert f'pollOp("{running_id}")' in html

    def test_non_admin_redirected(self, flask_app, client, plain_user):
        _login(client, flask_app, plain_user)
        resp = client.get("/admin/nodes")
        assert resp.status_code == 302
