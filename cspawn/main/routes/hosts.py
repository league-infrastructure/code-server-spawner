import json
from typing import cast

from flask import current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from cspawn.cs_docker.csmanager import CSMService
from cspawn.main import main_bp
from cspawn.models import CodeHost, ClassProto, db, User
from cspawn.init import cast_app
from cspawn.util.host_s3_sync import HostS3Sync
from cspawn.cs_github.repo import CodeHostRepo

ca = cast_app(current_app)


@main_bp.route("/host/<host_id>/stop", methods=["GET"])
@login_required
def stop_host(host_id) -> str:
    ca = cast_app(current_app)


    return_url = request.args.get("return_url", url_for("main.index"))

    if host_id == "mine":
        code_host = CodeHost.query.filter_by(user_id=current_user.id).first()
    else:
        code_host = CodeHost.query.get(host_id)

        if not code_host or code_host.user_id != current_user.id:
            flash("Host not found", "danger")
            return redirect(url_for("main.index"))

    if not code_host or code_host.user_id != current_user.id:
        flash("You do not have permission to stop this host.", "danger")
        return redirect(return_url)

    try:
        s = ca.csm.get(code_host.service_id)
    except KeyError:
        s = None

    if code_host:
        if not s:
            flash("Host not found", "danger")
            return redirect(url_for("admin.list_code_hosts"))

        s.stop()

        db.session.delete(code_host)
        db.session.commit()
        flash("Host stopped", "success")
    else:
        flash("Host not found", "danger")

    return redirect(return_url)


@main_bp.route("/host/<host_id>/open", methods=["GET"])
@login_required
def open_host(host_id) -> str:
    code_host = CodeHost.query.get(host_id)

    if not code_host:
        return jsonify({"success": False, "message": "Host record not found"})
    
    if not code_host or code_host.user_id != current_user.id:
        return jsonify({"success": False, "message": "You do not have permission to access this host."})
    
    
    s = ca.csm.get(code_host)

    if not s:
        # There was no service for the code host
        db.session.delete(code_host)
        db.session.commit()

        return jsonify({"success": False, "message": "Host service not found"})

    return jsonify({"success": True, "message": "Host found", "public_url": s.public_url})


@main_bp.route("/host/is_ready", methods=["GET"])
@login_required
def is_ready() -> jsonify:
    from docker.errors import NotFound

    try:
        host = CodeHost.query.filter_by(user_id=current_user.id).first()
        
        if not host:
            return jsonify({"status": "error", "message": "No host found"})

        s: CSMService = current_app.csm.get(host.service_id)

        s.sync_to_db()

        if s.check_ready():
            return jsonify({"status": "ready", "hostname_url": s.public_url})
        else:
            return jsonify({"status": "not_ready"})
    except (NotFound, AttributeError) as e:
        return jsonify({"status": "error", "message": str(e)})


@main_bp.route("/host/<chost_id>/open", methods=["GET"])
@login_required
def open_codehost(chost_id: str) -> str:
    ch = CodeHost.query.filter_by(id=chost_id).first()

    if not ch:
        flash("Service not found (a)", "error")
        return redirect(url_for("hosts.index"))

    if current_user.id != ch.user_id:
        flash("Service not found (b)", "error")
        return redirect(url_for("hosts.index"))

    return render_template("hosts/open_codehost.html", public_url=ch.public_url)




class PullError(Exception):
    pass

def _get_codehost(username, host_uuid=None):
    ca = cast_app(current_app)
    host_uuid = request.args.get("host_uuid") if host_uuid is None else host_uuid

    user: User = User.query.filter_by(username=username).first()
    if not user:
        raise PullError("User not found " + username)

    code_host: CodeHost = CodeHost.query.filter_by(user_id=user.id).first()
    if not code_host:
        raise PullError("Host not found " + str(host_uuid))

    if code_host.host_uuid != host_uuid:
        raise PullError("Permission denied")

    return code_host

@main_bp.route("/host/<username>/push", methods=["POST", "GET"])
def pull_from_host(username):
    """
    Pull changes from GitHub into the user's code host.
    Requires host_uuid as a query parameter for security.
    Optional: branch, rebase, dry_run as query parameters.
    """
    try:
        code_host = _get_codehost(username)
        app = cast_app(current_app)
        branch = request.args.get("branch", "master")
        rebase = request.args.get("rebase", "true").lower() == "true"
        dry_run = request.args.get("dry_run", "false").lower() == "true"

        ch_repo = CodeHostRepo.new_codehostrepo(app, username)
        ch_repo.push(branch=None)
        return jsonify({"status": "success", "message": "pushed"}, 200)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500