import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from functools import wraps
from operator import is_
from subprocess import DEVNULL

import docker
from flask import Response, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from cspawn.init import cast_app
from cspawn.models import Class, CodeHost, ClassProto, NodeOp, User, db

from . import admin_bp

ca = cast_app(current_app)


def _context():
    from cspawn.init import default_context  # Breaks circular import

    return default_context


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            if current_user.is_admin:
                return f(*args, **kwargs)

        except AttributeError:
            # Handle the case where current_user does not have is_admin attribute,
            # which may happen if the user is not authenticated
            pass

        return redirect(url_for("main.index"))

    return decorated_function


@admin_bp.route("/")
@admin_required
def index():
    # Gather dashboard stats
    num_code_hosts = CodeHost.query.count()
    num_running_hosts = CodeHost.query.filter_by(state="running").count()
    num_users = User.query.count()
    num_classes = Class.query.count()
    context = _context()
    context.update(
        {
            "num_code_hosts": num_code_hosts,
            "num_running_hosts": num_running_hosts,
            "num_users": num_users,
            "num_classes": num_classes,
        }
    )
    return render_template("admin/index.html", **context)


@admin_bp.route("/hosts")
@admin_required
def list_code_hosts():
    code_hosts = CodeHost.query.all()

    return render_template("admin/code_hosts.html", code_hosts=code_hosts)


@admin_bp.route("/hosts/sync", methods=["POST"])
@admin_required
def sync_code_hosts():
    """Reconcile the CodeHost DB rows against live Docker/swarm state.

    Manual trigger for the same reconcile the cron is meant to run: flips stale
    'unknown' rows to their real state, marks missing services MIA, and refreshes
    app readiness. Per-host failures are isolated inside csm.sync(), so one
    unreachable host does not abort the whole pass.
    """
    try:
        ca.csm.sync(check_ready=True)
        flash("Code hosts synchronized with Docker.", "success")
    except Exception as e:
        flash(f"Sync failed: {e}", "danger")
    return redirect(url_for("admin.list_code_hosts"))


@admin_bp.route("/host/<int:host_id>/delete", methods=["POST"])
@admin_required
def delete_host(host_id):
    code_host = CodeHost.query.get_or_404(host_id)
    db.session.delete(code_host)
    db.session.commit()
    flash("Host deleted successfully", "success")
    return redirect(url_for("admin.list_code_hosts"))


@admin_bp.route("/host/<int:host_id>/stop", methods=["POST"])
@admin_required
def stop_host(host_id):
    code_host = CodeHost.query.get(host_id)

    if not host_id:
        flash("No host ID provided", "danger")
        return redirect(url_for("admin.list_code_hosts"))

    code_host = CodeHost.query.get_or_404(host_id)
    s = ca.csm.get(code_host.service_id)
    if not s:
        flash("Host not found", "danger")
        db.session.delete(code_host)
        db.session.commit()
        return redirect(url_for("admin.list_code_hosts"))
    
    s.stop()
    db.session.delete(code_host)
    db.session.commit()
    flash("Host deleted successfully", "success")
    return redirect(url_for("admin.list_code_hosts"))


@admin_bp.route("/host/<int:host_id>/details", methods=["GET"])
@admin_required
def view_host(host_id):
    code_host = CodeHost.query.get_or_404(host_id)
    service = ca.csm.get(code_host.service_id)
    if not service:
        flash("Service not found", "danger")
        return redirect(url_for("admin.list_code_hosts"))
    return render_template("admin/view_host.html", code_host=code_host, service=service)


@admin_bp.route("/user/<int:user_id>/delete", methods=["GET", "POST"])
@admin_required
def delete_user(user_id):
    """Fully delete a user: stop servers, delete GitHub repos, delete the record.

    GET renders a confirmation page listing what will be torn down. POST (the
    Delete button on that page) performs the destructive teardown synchronously
    and reports a summary.
    """
    from cspawn.admin.teardown import teardown_user

    user = User.query.get_or_404(user_id)

    # Root and self protection: never delete the root user or the logged-in admin.
    if user.id == 0:
        flash("Refusing to delete the root user.", "danger")
        return redirect(url_for("admin.list_users"))
    if user.id == current_user.id:
        flash("Refusing to delete the currently logged-in admin.", "danger")
        return redirect(url_for("admin.list_users"))

    if request.method == "POST":
        force = "force" in request.form
        report = teardown_user(ca, user, force=force)

        summary = (
            f"Servers stopped: {len(report.servers_stopped)}; "
            f"repos deleted: {len(report.repos_deleted)}."
        )
        if report.user_deleted:
            flash(f"User '{report.username}' fully deleted. {summary}", "success")
        else:
            flash(
                f"User '{report.username}' NOT deleted (kept for retry). {summary}",
                "warning",
            )
        for f in report.failures:
            flash(f"Failure: {f}", "danger")

        if report.user_deleted:
            return redirect(url_for("admin.list_users"))
        return redirect(url_for("admin.delete_user", user_id=user_id))

    code_hosts = CodeHost.query.filter_by(user_id=user.id).all()
    return render_template("admin/delete_user.html", user=user, code_hosts=code_hosts)


@admin_bp.route("/protos")
@admin_required
def list_protos():
    protos = []
    for proto in ClassProto.query.all():
        code_host_count = CodeHost.query.filter_by(proto_id=proto.id).count()
        protos.append({"proto": proto, "code_host_count": code_host_count})
    return render_template("admin/protos.html", protos=protos)


@admin_bp.route("/proto/<int:proto_id>", methods=["GET", "POST"])
@admin_required
def edit_proto(proto_id):
    proto = ClassProto.query.get_or_404(proto_id)
    has_code_hosts = CodeHost.query.filter_by(proto_id=proto_id).count() > 0
    if request.method == "POST":
        proto.name = request.form["name"]
        proto.desc = request.form["description"]
        proto.image_uri = request.form["image_uri"]
        proto.repo_uri = request.form["repo_uri"]
        proto.syllabus_path = request.form["syllabus_path"]
        proto.is_public = "is_public" in request.form
        db.session.commit()
        flash("Proto updated successfully", "success")
        return redirect(url_for("admin.list_protos"))
    return render_template("admin/edit_proto.html", proto=proto, has_code_hosts=has_code_hosts)


@admin_bp.route("/proto/new", methods=["GET", "POST"])
@admin_required
def new_proto():
    if request.method == "POST":
        new_proto = ClassProto(
            name=request.form["name"],
            image_uri=request.form["image_uri"],
            repo_uri=request.form["repo_uri"],
            is_public="is_public" in request.form,
            creator_id=current_user.id,
        )
        db.session.add(new_proto)
        db.session.commit()
        flash("New proto created successfully", "success")
        return redirect(url_for("admin.list_protos"))
    return render_template("admin/edit_proto.html", proto=None, has_code_hosts=False)


@admin_bp.route("/proto/<int:proto_id>/delete", methods=["POST"])
@admin_required
def delete_proto(proto_id):
    proto = ClassProto.query.get_or_404(proto_id)
    db.session.delete(proto)
    db.session.commit()
    flash("Proto deleted successfully", "success")
    return redirect(url_for("admin.list_protos"))


@admin_bp.route("/protos/export", methods=["GET"])
@admin_required
def export_protos():
    protos = ClassProto.query.all()
    proto_data = [
        {
            "name": proto.name,
            "image_uri": proto.image_uri,
            "repo_uri": proto.repo_uri,
            "is_public": proto.is_public,
            "creator_id": proto.creator_id,
        }
        for proto in protos
    ]
    response = current_app.response_class(
        response=json.dumps(proto_data),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment;filename=protos.json"},
    )
    return response


@admin_bp.route("/protos/import", methods=["GET", "POST"])
@admin_required
def import_protos():
    if request.method == "POST":
        if "file" not in request.files:
            flash("No file part", "danger")
            return redirect(url_for("admin.import_protos"))

        file = request.files["file"]
        if file.filename == "":
            flash("No selected file", "danger")
            return redirect(url_for("admin.import_protos"))

        if file and file.filename.endswith(".json"):
            try:
                proto_data = json.load(file)
                for proto in proto_data:
                    if not ClassProto.query.filter_by(
                        image_uri=proto["image_uri"], repo_uri=proto["repo_uri"], creator_id=proto["creator_id"]
                    ).first():
                        new_proto = ClassProto(
                            name=proto["name"],
                            image_uri=proto["image_uri"],
                            repo_uri=proto["repo_uri"],
                            is_public=proto["is_public"],
                            creator_id=proto["creator_id"],
                        )
                        db.session.add(new_proto)
                db.session.commit()
                flash("Protos imported successfully", "success")
            except Exception as e:
                db.session.rollback()
                flash(f"An error occurred: {str(e)}", "danger")
        else:
            flash("Invalid file format", "danger")

        return redirect(url_for("admin.list_protos"))

    return render_template("admin/import_protos.html")


@admin_bp.route("/users")
@admin_required
def list_users():
    users = User.query.all()
    current_year = datetime.now().year
    return render_template("admin/users.html", users=users, current_year=current_year)


@admin_bp.route("/users/<int:user_id>/impersonate", methods=["POST"])
@admin_required
def impersonate_user(user_id: int):
    if current_user.id == user_id:
        flash("You're already signed in as that user.", "info")
        return redirect(url_for("admin.list_users"))

    target_user = User.query.get_or_404(user_id)

    if not session.get("impersonator_id"):
        session["impersonator_id"] = current_user.id
        session["impersonator_name"] = (
            current_user.display_name or current_user.username or current_user.email or "admin"
        )

    login_user(target_user)
    flash(
        f"Now impersonating {target_user.display_name or target_user.username or target_user.email}",
        "success",
    )
    return redirect(url_for("main.index"))


@admin_bp.route("/users/stop-impersonating", methods=["POST"])
@login_required
def stop_impersonating():
    impersonator_id = session.pop("impersonator_id", None)
    impersonator_name = session.pop("impersonator_name", None)

    if not impersonator_id:
        flash("You're not impersonating another user.", "info")
        return redirect(url_for("main.index"))

    original_user = User.query.get(impersonator_id)
    if not original_user:
        flash("Original admin account could not be restored. Please log in again.", "danger")
        logout_user()
        return redirect(url_for("auth.logout"))

    login_user(original_user)
    flash(
        f"Returned to {impersonator_name or original_user.display_name or original_user.username or 'admin'}.",
        "success",
    )
    return redirect(url_for("admin.list_users"))


@admin_bp.route("/classes")
@admin_required
def classes():
    classes = Class.query.all()
    return render_template("admin/classes.html", classes=classes)


@admin_bp.route("/classes/export", methods=["GET"])
@admin_required
def export_classes():
    classes = Class.query.all()
    class_data = [
        {
            "name": class_.name,
            "description": class_.description,
            "class_code": class_.class_code,
            "proto_id": class_.image_id,
            "start_date": class_.start_date.isoformat(),
        }
        for class_ in classes
    ]
    response = current_app.response_class(
        response=json.dumps(class_data),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment;filename=classes.json"},
    )
    return response


@admin_bp.route("/classes/<class_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_class(class_id):
    from cspawn.main.routes.classes import _edit_class

    return _edit_class(class_id, "admin.classes")


@admin_bp.route("/classes/<int:class_id>/delete")
@admin_required
def delete_class(class_id):
    if not current_user.is_instructor:
        return redirect(url_for("admin.classes"))

    class_ = Class.query.get(class_id)
    if class_:
        # Remove all students and instructors from the class
        class_.students.clear()
        class_.instructors.clear()
        db.session.commit()  # Commit the changes to update the relationships

        db.session.delete(class_)
        db.session.commit()
        flash("Class deleted.")

    return redirect(url_for("admin.classes"))


# ---------------------------------------------------------------------------
# Helper: resolve cspawnctl executable path
# ---------------------------------------------------------------------------

def _cspawnctl_path() -> str:
    """Return path to the cspawnctl executable.

    Tries shutil.which first (covers PATH installs and pipx).
    Falls back to sys.argv[0]-based heuristic (same directory as the running
    process) for the editable / dev case.
    """
    found = shutil.which("cspawnctl")
    if found:
        return found
    # Editable install: cspawnctl lives next to the running interpreter
    import os
    bin_dir = os.path.dirname(sys.executable) if sys.executable else ""
    if bin_dir:
        candidate = os.path.join(bin_dir, "cspawnctl")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    # Last resort: rely on PATH at subprocess time
    return "cspawnctl"


# ---------------------------------------------------------------------------
# GET /admin/nodes — list swarm nodes
# ---------------------------------------------------------------------------

@admin_bp.route("/nodes")
@admin_required
def list_nodes():
    """List all swarm nodes with host counts, tiers, and recent operations."""
    from cspawn.cli.node import count_hosts_per_node
    from cspawn.cs_docker.tiers import load_tiers

    docker_uri = ca.app_config.get("DOCKER_URI")
    node_rows = []
    try:
        client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
        host_counts = count_hosts_per_node(client)
        for n in client.nodes.list():
            spec = n.attrs.get("Spec", {})
            desc = n.attrs.get("Description", {})
            status = n.attrs.get("Status", {})
            ms = n.attrs.get("ManagerStatus") or {}
            labels = spec.get("Labels") or {}
            hostname = desc.get("Hostname", "")
            role = (spec.get("Role") or "worker").lower()
            is_leader = bool(ms.get("Leader"))
            short = hostname.split(".")[0]
            node_rows.append({
                "hostname": hostname,
                "short": short,
                "ip": status.get("Addr", ""),
                "role": "leader" if is_leader else role,
                "tier": labels.get("cs.tier", ""),
                "capacity": labels.get("cs.capacity", ""),
                "host_count": host_counts.get(short, 0),
                "availability": spec.get("Availability", ""),
                "is_manager": role == "manager",
                "is_leader": is_leader,
            })
        client.close()
    except Exception as e:
        flash(f"Could not connect to Docker: {e}", "danger")

    tiers = load_tiers(ca.app_config)
    recent_ops = NodeOp.query.order_by(NodeOp.created_at.desc()).limit(20).all()
    return render_template("admin/nodes.html", node_rows=node_rows, tiers=tiers, recent_ops=recent_ops)


# ---------------------------------------------------------------------------
# POST /admin/nodes/start — launch an expand operation
# ---------------------------------------------------------------------------

@admin_bp.route("/nodes/start", methods=["POST"])
@admin_required
def nodes_start():
    """Create a NodeOp(kind='expand') and launch cspawnctl node op-run detached."""
    from cspawn.cs_docker.tiers import load_tiers

    tier_name = request.form.get("tier", "").strip()
    tiers = load_tiers(ca.app_config)
    valid_tier_names = {t.name for t in tiers}

    if not tier_name or tier_name not in valid_tier_names:
        flash(f"Invalid tier: {tier_name!r}. Choose one of: {', '.join(sorted(valid_tier_names))}", "danger")
        return redirect(url_for("admin.list_nodes"))

    op = NodeOp(
        kind="expand",
        tier=tier_name,
        status="pending",
        created_by=current_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(op)
    db.session.commit()

    deploy = ca.app_config.get("JTL_DEPLOYMENT", "devel")
    cspawnctl = _cspawnctl_path()
    subprocess.Popen(
        [cspawnctl, "-d", deploy, "node", "op-run", str(op.id)],
        start_new_session=True,
        stdout=DEVNULL,
        stderr=DEVNULL,
    )

    flash(f"Starting node expansion (tier={tier_name}, op {op.id})", "success")
    return redirect(url_for("admin.list_nodes"))


# ---------------------------------------------------------------------------
# POST /admin/nodes/remove — launch a remove operation
# ---------------------------------------------------------------------------

@admin_bp.route("/nodes/remove", methods=["POST"])
@admin_required
def nodes_remove():
    """Validate node is not manager/leader, then create NodeOp(kind='remove') and launch detached."""
    fqdn = request.form.get("fqdn", "").strip()
    if not fqdn:
        flash("No FQDN provided.", "danger")
        return redirect(url_for("admin.list_nodes"))

    # Re-query Docker to confirm node is not manager/leader
    docker_uri = ca.app_config.get("DOCKER_URI")
    try:
        client = docker.DockerClient(base_url=docker_uri, use_ssh_client=True)
        short = fqdn.split(".")[0]
        for n in client.nodes.list():
            desc = n.attrs.get("Description", {})
            hostname = desc.get("Hostname", "")
            if hostname == fqdn or hostname.split(".")[0] == short:
                spec = n.attrs.get("Spec", {})
                role = (spec.get("Role") or "worker").lower()
                ms = n.attrs.get("ManagerStatus") or {}
                is_leader = bool(ms.get("Leader"))
                if role == "manager" or is_leader:
                    client.close()
                    flash(
                        f"Cannot remove {fqdn}: node is a swarm manager/leader. "
                        "Demote it first.",
                        "danger",
                    )
                    return redirect(url_for("admin.list_nodes"))
                break
        client.close()
    except Exception as e:
        flash(f"Could not validate node against Docker: {e}", "danger")
        return redirect(url_for("admin.list_nodes"))

    op = NodeOp(
        kind="remove",
        target_fqdn=fqdn,
        status="pending",
        created_by=current_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(op)
    db.session.commit()

    deploy = ca.app_config.get("JTL_DEPLOYMENT", "devel")
    cspawnctl = _cspawnctl_path()
    subprocess.Popen(
        [cspawnctl, "-d", deploy, "node", "op-run", str(op.id)],
        start_new_session=True,
        stdout=DEVNULL,
        stderr=DEVNULL,
    )

    flash(f"Removing node {fqdn} (op {op.id})", "success")
    return redirect(url_for("admin.list_nodes"))


# ---------------------------------------------------------------------------
# GET /admin/nodes/op/<op_id>/status — JSON poll endpoint
# ---------------------------------------------------------------------------

@admin_bp.route("/nodes/op/<op_id>/status")
@admin_required
def node_op_status(op_id):
    """Return JSON {status, exit_code, message, log_tail} for polling."""
    op = NodeOp.query.get(op_id)
    if op is None:
        abort(404)

    log_tail = ""
    if op.log_path:
        try:
            with open(op.log_path, "r", errors="replace") as fh:
                lines = fh.readlines()
                log_tail = "".join(lines[-50:])
        except OSError:
            log_tail = ""

    return jsonify({
        "status": op.status,
        "exit_code": op.exit_code,
        "message": op.message,
        "log_tail": log_tail,
    })


# ---------------------------------------------------------------------------
# GET /admin/nodes/op/<op_id>/log — full plain-text log
# ---------------------------------------------------------------------------

@admin_bp.route("/nodes/op/<op_id>/log")
@admin_required
def node_op_log(op_id):
    """Return the full plain-text log for a NodeOp."""
    op = NodeOp.query.get(op_id)
    if op is None:
        abort(404)

    log_text = ""
    if op.log_path:
        try:
            with open(op.log_path, "r", errors="replace") as fh:
                log_text = fh.read()
        except OSError:
            log_text = ""

    return Response(log_text, mimetype="text/plain")
