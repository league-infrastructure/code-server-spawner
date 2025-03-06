import time
from typing import cast

import docker
from flask import (
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from cspawn.util.apptypes import App
from cspawn.main.models import HostImage
from cspawn.hosts import hosts_bp
from cspawn.main.models import CodeHost, db
from cspawn.docker.csmanager import CSMService

import json

ca = cast(App, current_app)


@hosts_bp.route("/")
@login_required
def index() -> str:

    ch = CodeHost.query.filter_by(user_id=current_user.id).first()  # extant code host

    s: CSMService = ca.csm.get(ch.service_id) if ch else None

    if s:
        ch: CodeHost = s.sync_to_db(check_ready=True)  # update the host record

    host_images = HostImage.query.all()

    # If we have a code host, it is the only one shown on the list.
    if ch:
        for i, host_image in enumerate(host_images):
            if host_image.id == ch.host_image_id:
                host_images = [host_image]
                break

    return render_template("hosts/image_host_list.html", host=ch, host_images=host_images)


@hosts_bp.route("/start")
@login_required
def start_host() -> str:
    image_id = request.args.get("image_id")
    image = HostImage.query.get(image_id)

    import logging
    logger = logging.getLogger("cspawn.docker")
    logger.setLevel(logging.DEBUG)

    if not image:
        flash("Image not found", "error")
        return redirect(url_for("hosts.index"))
    # Look for an existing CodeHost for the current user
    extant_host = CodeHost.query.filter_by(user_id=current_user.id).first()

    if extant_host:
        flash("A host is already running for the current user", "info")
        return redirect(url_for("hosts.index"))

    # Create a new CodeHost instance
    s = ca.csm.get_by_username(current_user.username)

    if not s:
        s = ca.csm.new_cs(
            user=current_user,
            image=image.image_uri,
            repo=image.repo_uri,
            syllabus=image.syllabus_path,
        )

        flash(f"Host {s.name} started successfully", "success")
    else:
        s.sync_to_db()
        flash("Host already running", "info")

    return redirect(url_for("hosts.index"))


@hosts_bp.route("/stop")
@login_required
def stop_host() -> str:
    host_id = request.args.get("host_id")

    if not host_id:
        flash("No host found to stop (no host_id)", "error")
        return redirect(url_for("hosts.index"))

    ch = CodeHost.query.filter_by(user_id=current_user.id).first()

    if not ch:
        flash("No host found to stop (no host record)", "error")
        return redirect(url_for("hosts.index"))

    if host_id and str(ch.id) != str(host_id):
        flash(
            f"Host stop disallowed (host id mismatch {host_id} != {ch.id})",
            "error",
        )
        return redirect(url_for("hosts.index"))

    s = ca.csm.get(ch.service_id)

    if not s:
        flash("No server found to stop", "error")
        return redirect(url_for("hosts.index"))

    s.stop()

    db.session.delete(ch)
    db.session.commit()

    flash("Server stopped successfully", "success")
    return redirect(url_for("hosts.index"))


@hosts_bp.route("is_ready", methods=["GET"])
@login_required
def is_ready() -> jsonify:
    from docker.errors import NotFound

    try:
        host = CodeHost.query.filter_by(user_id=current_user.id).first()

        if not host:
            return jsonify({"status": "error", "message": "No host found"})

        s = ca.csm.get(host.service_id)

        s.sync_to_db()

        if s.check_ready():
            return jsonify({"status": "ready", "hostname_url": s.public_url})
        else:
            return jsonify({"status": "not_ready"})
    except NotFound as e:
        return jsonify({"status": "error", "message": str(e)})


@hosts_bp.route("/service/<chost_id>/open", methods=["GET"])
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
