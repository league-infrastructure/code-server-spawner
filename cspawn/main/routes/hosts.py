import json
from typing import cast

from flask import current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from cspawn.cs_docker.csmanager import CSMService
from cspawn.main import main_bp
from cspawn.models import CodeHost, ClassProto, db
from cspawn.init import cast_app

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
