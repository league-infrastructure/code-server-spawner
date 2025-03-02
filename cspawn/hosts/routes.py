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
from cspawn.docker.models import CodeHost, HostImage
from cspawn.hosts import hosts_bp
from cspawn.main.models import db
import json

ca = cast(App, current_app)


@hosts_bp.route("/")
@login_required
def index() -> str:
    iframe_url = "http://jointheleague.org"  # Replace with the actual URL you want to load in the iframe

    host_rec = CodeHost.query.filter_by(user_id=current_user.id).first()

    if host_rec:
        s = ca.csm.get(host_rec.service_id)

        return render_template(
            "hosts/index_running.html", host=host_rec, service=s, iframe_url=iframe_url
        )
    else:
        host_images = HostImage.query.all()
        return render_template("hosts/index_stopped.html", host_images=host_images)


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


@hosts_bp.route("/service/<service_id>/is_ready", methods=["GET"])
@login_required
def is_ready(service_id: str) -> jsonify:
    from docker.errors import NotFound

    try:
        host = CodeHost.query.filter_by(user_id=current_user.id).first()

        s = ca.csm.get(service_id)

        if host and host.service_id != service_id:
            return jsonify({"status": "error", "message": "Service ID mismatch"})

        if s.check_ready():
            return jsonify({"status": "ready", "hostname_url": s.hostname_url})
        else:
            return jsonify({"status": "not_ready"})
    except NotFound as e:
        return jsonify({"status": "error", "message": str(e)})


@hosts_bp.route("/loading")
def loading() -> str:
    return render_template("hosts/loading.html")
