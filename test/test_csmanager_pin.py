"""
Unit tests for the create-time node-pin call site (sprint 014, ticket 001):

    cspawn/cs_docker/csmanager.py::_truthy
    cspawn/cs_docker/csmanager.py::CodeServerManager._new_cs_inner()

No existing test file exercises `_new_cs_inner()`/`new_cs()` at all before
this ticket. No live Docker daemon in any test here — everything is mocked,
following the `CodeServerManager.__new__(CodeServerManager)` + manual
attribute injection pattern established in `test/test_stop_host.py`'s
`_make_manager(app)`, and the MagicMock service/node builder style in
`test/test_node_missing.py`.

Run with::

    uv run pytest test/test_csmanager_pin.py -v
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cspawn.cs_docker.csmanager import CodeServerManager, _truthy
from cspawn.models import ClassProto, CodeHost, User
from cspawn.util.config import Config


# ---------------------------------------------------------------------------
# Shared fixtures — in-memory SQLite Flask app + minimal User/ClassProto rows
# ---------------------------------------------------------------------------

def _make_flask_app():
    """Create a minimal in-memory Flask app wired to cspawn models."""
    from flask import Flask
    from cspawn.models import db as _db

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SECRET_KEY"] = "test-csmanager-pin-secret"
    app.config["TESTING"] = True

    _db.init_app(app)
    with app.app_context():
        _db.create_all()

    return app, _db


def _make_manager(app, *, pin_hosts_to_node=True, placement_timeout_s=None, client=None):
    """Build a CodeServerManager without touching Docker or GitHub.

    CodeServerManager.__init__ connects to a real Docker daemon (unavailable
    in unit tests), so we bypass it entirely, exactly as
    test_stop_host.py's _make_manager(app) does for stop_host()/remove_all().

    self.config must support attribute-style access with the getattr(...,
    default) idiom the pin block uses (`getattr(self.config,
    "PIN_HOSTS_TO_NODE", True)`), so it's a real `cspawn.util.config.Config`
    -- a plain dict would make every getattr() miss and always return the
    caller's default, defeating the on/off tests below.
    """
    cfg_dict = {
        "HOSTNAME_TEMPLATE": "{username}.example.com:{port}",
        "USER_DIRS": "",  # falsy -> _new_cs_inner skips make_user_dir's SSH call
        "PIN_HOSTS_TO_NODE": pin_hosts_to_node,
    }
    if placement_timeout_s is not None:
        cfg_dict["PIN_HOST_PLACEMENT_TIMEOUT_S"] = placement_timeout_s

    csm = CodeServerManager.__new__(CodeServerManager)
    csm.app = app
    csm.config = Config(cfg_dict)
    csm.client = client or MagicMock()
    return csm


_counter = 0


def _make_user_and_proto(db):
    """Create one User + ClassProto row. Returns (user, proto)."""
    global _counter
    _counter += 1
    n = _counter

    user = User(
        user_id=f"uid-pin{n}",
        email=f"pin{n}@example.com",
        username=f"pin{n}",
        is_active=True,
    )
    db.session.add(user)
    db.session.flush()

    proto = ClassProto(
        name="proto",
        hash="hash",
        image_uri="example/image:latest",
        repo_uri="https://github.com/example/repo.git",
        syllabus_path=None,
    )
    db.session.add(proto)
    db.session.commit()

    return user, proto


def _make_fake_cs_service(user, *, service_id):
    """A stand-in for the CSMService that CodeServerManager.run() returns.

    `.o` is the raw docker-py Service object (`CSMService.o`, per
    `cs_docker/proc.py`), forwarded to `_resolve_task_node_fqdn`/
    `_pin_service_to_node` -- its identity is asserted on below, its
    contents never matter since those two helpers are mocked out here.
    """
    fake = MagicMock()
    fake.o = MagicMock(name=f"raw-service-{service_id}")

    def _to_model(no_container=False):
        return CodeHost(
            user_id=user.id,
            service_id=service_id,
            service_name=user.username,
            state="running",
        )

    fake.to_model.side_effect = _to_model
    return fake


def _patch_creation_deps(container_def=None):
    """Patch the GitHub-org fork and container-spec builder _new_cs_inner()
    calls before self.run() -- both unrelated to this ticket's scope, and
    heavy (network/config-shaped) if left unmocked."""
    gorg = MagicMock()
    gorg.fork.return_value = MagicMock()
    return (
        patch("cspawn.cs_docker.csmanager.GithubOrg.new_org", return_value=gorg),
        patch(
            "cspawn.cs_docker.csmanager.define_cs_container",
            return_value=container_def or {"name": "svc", "image": "img"},
        ),
    )


def _make_conflict_error(status_code=409):
    """Build a docker.errors.APIError carrying a real `.response.status_code`,
    matching what `self.run()` raises on the 409-already-exists race."""
    import docker.errors
    from requests.models import Response

    resp = Response()
    resp.status_code = status_code
    return docker.errors.APIError("conflict", response=resp)


# ---------------------------------------------------------------------------
# _truthy
# ---------------------------------------------------------------------------

class TestTruthy:
    def test_none_returns_default(self):
        assert _truthy(None, True) is True
        assert _truthy(None, False) is False

    def test_bool_passthrough(self):
        assert _truthy(True, False) is True
        assert _truthy(False, True) is False

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "YES"])
    def test_truthy_strings(self, value):
        assert _truthy(value, False) is True

    @pytest.mark.parametrize("value", ["false", "False", "0", "no", "", "garbage"])
    def test_falsy_strings(self, value):
        assert _truthy(value, True) is False


# ---------------------------------------------------------------------------
# _new_cs_inner — pin call site
# ---------------------------------------------------------------------------

class TestNewCsInnerPin:
    def test_pin_applied_when_enabled_and_resolution_succeeds(self):
        """PIN_HOSTS_TO_NODE on (default) + a resolved node -> the service is
        pinned to it, and CodeHost.node_name is populated immediately."""
        app, db = _make_flask_app()
        with app.app_context():
            user, proto = _make_user_and_proto(db)
            csm = _make_manager(app, pin_hosts_to_node=True, placement_timeout_s=3.0)
            fake_service = _make_fake_cs_service(user, service_id="svc-ok")
            csm.run = MagicMock(return_value=fake_service)

            gorg_patch, container_patch = _patch_creation_deps()
            with gorg_patch, container_patch, \
                 patch("cspawn.cli.node._resolve_task_node_fqdn",
                       return_value="swarm3.example.com") as mock_resolve, \
                 patch("cspawn.cli.node._pin_service_to_node") as mock_pin:
                s, ch = csm._new_cs_inner(user, proto, None)

            assert s is fake_service
            mock_resolve.assert_called_once()
            args, kwargs = mock_resolve.call_args
            assert args[0] is csm.client
            assert args[1] is fake_service.o
            assert kwargs["timeout"] == 3.0
            mock_pin.assert_called_once_with(fake_service.o, "swarm3.example.com")
            assert ch.node_name == "swarm3.example.com"
            assert CodeHost.query.filter_by(service_id="svc-ok").first().node_name == "swarm3.example.com"

    def test_pin_skipped_when_flag_disabled(self):
        """PIN_HOSTS_TO_NODE=False fully restores today's behavior: no resolve
        poll, no pin call, node_name stays None."""
        app, db = _make_flask_app()
        with app.app_context():
            user, proto = _make_user_and_proto(db)
            csm = _make_manager(app, pin_hosts_to_node=False)
            fake_service = _make_fake_cs_service(user, service_id="svc-off")
            csm.run = MagicMock(return_value=fake_service)

            gorg_patch, container_patch = _patch_creation_deps()
            with gorg_patch, container_patch, \
                 patch("cspawn.cli.node._resolve_task_node_fqdn") as mock_resolve, \
                 patch("cspawn.cli.node._pin_service_to_node") as mock_pin:
                s, ch = csm._new_cs_inner(user, proto, None)

            mock_resolve.assert_not_called()
            mock_pin.assert_not_called()
            assert ch.node_name is None

    def test_resolution_timeout_is_best_effort_no_crash(self, caplog):
        """_resolve_task_node_fqdn returning None (timeout) logs a WARNING,
        skips the pin call, and still returns the created host normally."""
        app, db = _make_flask_app()
        with app.app_context():
            user, proto = _make_user_and_proto(db)
            csm = _make_manager(app, pin_hosts_to_node=True)
            fake_service = _make_fake_cs_service(user, service_id="svc-timeout")
            csm.run = MagicMock(return_value=fake_service)

            gorg_patch, container_patch = _patch_creation_deps()
            with gorg_patch, container_patch, \
                 patch("cspawn.cli.node._resolve_task_node_fqdn",
                       return_value=None) as mock_resolve, \
                 patch("cspawn.cli.node._pin_service_to_node") as mock_pin, \
                 caplog.at_level("WARNING", logger="cspawn.docker"):
                s, ch = csm._new_cs_inner(user, proto, None)

            mock_resolve.assert_called_once()
            mock_pin.assert_not_called()
            assert s is fake_service
            assert ch is not None
            assert ch.node_name is None
            assert any(
                rec.levelname == "WARNING" and rec.name == "cspawn.docker"
                for rec in caplog.records
            )

    def test_pin_raising_is_best_effort_host_still_created(self, caplog):
        """_pin_service_to_node raising is caught, logged as a WARNING, and
        never blocks host creation -- the host is still committed and
        returned."""
        app, db = _make_flask_app()
        with app.app_context():
            user, proto = _make_user_and_proto(db)
            csm = _make_manager(app, pin_hosts_to_node=True)
            fake_service = _make_fake_cs_service(user, service_id="svc-pinfail")
            csm.run = MagicMock(return_value=fake_service)

            gorg_patch, container_patch = _patch_creation_deps()
            with gorg_patch, container_patch, \
                 patch("cspawn.cli.node._resolve_task_node_fqdn",
                       return_value="swarm7.example.com"), \
                 patch("cspawn.cli.node._pin_service_to_node",
                       side_effect=RuntimeError("swarm boom")), \
                 caplog.at_level("WARNING", logger="cspawn.docker"):
                s, ch = csm._new_cs_inner(user, proto, None)

            assert s is fake_service
            assert ch is not None
            assert CodeHost.query.filter_by(service_id="svc-pinfail").first() is not None
            assert any(
                rec.levelname == "WARNING" and rec.name == "cspawn.docker"
                for rec in caplog.records
            )

    def test_pin_exception_never_misrouted_into_409_handler(self, caplog):
        """A pin-block exception must be swallowed by the block's own
        try/except -- never escape into the enclosing `except
        docker.errors.APIError` (409-recovery) handler. Simulated by having
        _pin_service_to_node raise docker.errors.APIError itself (e.g. from
        svc.update()) and confirming the *fresh* host (not a 409-recovery
        lookup) is still returned, with _get_by_username_raw never invoked."""
        app, db = _make_flask_app()
        with app.app_context():
            user, proto = _make_user_and_proto(db)
            csm = _make_manager(app, pin_hosts_to_node=True)
            fake_service = _make_fake_cs_service(user, service_id="svc-apierr")
            csm.run = MagicMock(return_value=fake_service)
            csm._get_by_username_raw = MagicMock(
                side_effect=AssertionError("409-recovery path must not run")
            )

            gorg_patch, container_patch = _patch_creation_deps()
            with gorg_patch, container_patch, \
                 patch("cspawn.cli.node._resolve_task_node_fqdn",
                       return_value="swarm1.example.com"), \
                 patch("cspawn.cli.node._pin_service_to_node",
                       side_effect=_make_conflict_error()), \
                 caplog.at_level("WARNING", logger="cspawn.docker"):
                s, ch = csm._new_cs_inner(user, proto, None)

            assert s is fake_service
            assert ch is not None
            csm._get_by_username_raw.assert_not_called()

    def test_not_reapplied_on_409_recovery_path(self):
        """The 409-recovery branch (self.run() raising, an existing service
        found via _get_by_username_raw) never runs the pin block: no
        redundant reschedule of an already-pinned host."""
        app, db = _make_flask_app()
        with app.app_context():
            user, proto = _make_user_and_proto(db)
            csm = _make_manager(app, pin_hosts_to_node=True)
            csm.run = MagicMock(side_effect=_make_conflict_error())

            existing_service = _make_fake_cs_service(user, service_id="svc-409")
            csm._get_by_username_raw = MagicMock(return_value=existing_service)

            gorg_patch, container_patch = _patch_creation_deps()
            with gorg_patch, container_patch, \
                 patch("cspawn.cli.node._resolve_task_node_fqdn") as mock_resolve, \
                 patch("cspawn.cli.node._pin_service_to_node") as mock_pin:
                s, ch = csm._new_cs_inner(user, proto, None)

            assert s is existing_service
            mock_resolve.assert_not_called()
            mock_pin.assert_not_called()
            assert ch.node_name is None

    def test_idempotent_retried_start_does_not_double_pin(self):
        """A retried 'Start' hitting the 409-recovery path twice in a row
        never triggers a pin call at all -- relies on the 409 branch itself
        skipping the pin block (not on _pin_service_to_node's own
        replace-not-accumulate normalization, which is exercised elsewhere)."""
        app, db = _make_flask_app()
        with app.app_context():
            user, proto = _make_user_and_proto(db)
            csm = _make_manager(app, pin_hosts_to_node=True)
            csm.run = MagicMock(side_effect=_make_conflict_error())

            existing_service = _make_fake_cs_service(user, service_id="svc-retry")
            csm._get_by_username_raw = MagicMock(return_value=existing_service)

            gorg_patch, container_patch = _patch_creation_deps()
            with gorg_patch, container_patch, \
                 patch("cspawn.cli.node._resolve_task_node_fqdn") as mock_resolve, \
                 patch("cspawn.cli.node._pin_service_to_node") as mock_pin:
                csm._new_cs_inner(user, proto, None)
                csm._new_cs_inner(user, proto, None)

            mock_resolve.assert_not_called()
            mock_pin.assert_not_called()

    def test_default_placement_timeout_is_ten_seconds(self):
        """PIN_HOST_PLACEMENT_TIMEOUT_S left unset -> the documented default
        (10.0) is forwarded to _resolve_task_node_fqdn."""
        app, db = _make_flask_app()
        with app.app_context():
            user, proto = _make_user_and_proto(db)
            csm = _make_manager(app, pin_hosts_to_node=True)  # no placement_timeout_s override
            fake_service = _make_fake_cs_service(user, service_id="svc-defaulttimeout")
            csm.run = MagicMock(return_value=fake_service)

            gorg_patch, container_patch = _patch_creation_deps()
            with gorg_patch, container_patch, \
                 patch("cspawn.cli.node._resolve_task_node_fqdn",
                       return_value="swarm4.example.com") as mock_resolve, \
                 patch("cspawn.cli.node._pin_service_to_node"):
                csm._new_cs_inner(user, proto, None)

            assert mock_resolve.call_args.kwargs["timeout"] == 10.0


# ---------------------------------------------------------------------------
# Regression: the pin, once applied, is the exact constraint shape Swarm
# needs to refuse a migration (SUC-002) -- exercised against the real,
# unmocked _pin_service_to_node so this locks in its actual output shape.
# ---------------------------------------------------------------------------

class TestPinConstraintShapeRegression:
    def test_pinned_service_carries_exactly_one_hostname_constraint(self):
        from cspawn.cli.node import _pin_service_to_node

        app, db = _make_flask_app()
        with app.app_context():
            user, proto = _make_user_and_proto(db)
            csm = _make_manager(app, pin_hosts_to_node=True)
            fake_service = _make_fake_cs_service(user, service_id="svc-constraint")
            # Raw docker-py Service stand-in with a real placement-constraints
            # shape (["node.role==worker"], as define_cs_container sets it),
            # so _pin_service_to_node's actual add/replace logic runs for real.
            fake_service.o = MagicMock()
            fake_service.o.attrs = {
                "Spec": {"TaskTemplate": {"Placement": {"Constraints": ["node.role==worker"]}}}
            }
            csm.run = MagicMock(return_value=fake_service)

            gorg_patch, container_patch = _patch_creation_deps()
            with gorg_patch, container_patch, \
                 patch("cspawn.cli.node._resolve_task_node_fqdn",
                       return_value="swarm5.example.com"):
                # _pin_service_to_node is imported lazily inside _new_cs_inner
                # from cspawn.cli.node -- patch it there, but let it run for
                # real (no mock replacing its body) to lock in its output.
                with patch("cspawn.cli.node._pin_service_to_node", wraps=_pin_service_to_node) as spy:
                    csm._new_cs_inner(user, proto, None)

            spy.assert_called_once_with(fake_service.o, "swarm5.example.com")
            fake_service.o.update.assert_called_once()
            _, kwargs = fake_service.o.update.call_args
            constraints = kwargs["constraints"]
            hostname_constraints = [c for c in constraints if "node.hostname==" in c.replace(" ", "")]
            assert hostname_constraints == ["node.hostname==swarm5.example.com"]
            # The unrelated pre-existing constraint survives untouched.
            assert "node.role==worker" in constraints
