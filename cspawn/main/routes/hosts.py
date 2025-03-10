from typing import cast

from flask import (current_app, flash, jsonify, redirect, render_template,
                   request, url_for)
from flask_login import current_user, login_required

from cspawn.main import main_bp
from cspawn.models import CodeHost, HostImage


@main_bp.route("/hosts")
@login_required
def hosts() -> str:
    from cspawn.docker.csmanager import CSMService

    ch = CodeHost.query.filter_by(user_id=current_user.id).first()  # extant code host

    s: CSMService = ca().csm.get(ch.service_id) if ch else None

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


@main_bp.route("/host/start")
@login_required
def start_host() -> str:
    from cspawn.init import cast_app

    ca = cast_app(current_app)

    image_id = request.args.get("image_id")
    image = HostImage.query.get(image_id)

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


@main_bp.route("/host/<host_id>/stop", methods=["GET"])
@login_required
def stop_host(host_id) -> str:
    from cspawn.models import CodeHost, db
    from cspawn.init import cast_app

    ca = cast_app(current_app)

    return_url = request.args.get('return_url', url_for("main.index"))

    if host_id == 'mine':
        code_host = CodeHost.query.filter_by(user_id=current_user.id).first()
    else:

        code_host = CodeHost.query.get(host_id)

        if not code_host or code_host.user_id != current_user.id:
            flash("Host not found", "danger")
            return redirect(url_for("main.index"))

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


@main_bp.route("/host/is_ready", methods=["GET"])
@login_required
def is_ready() -> jsonify:
    from docker.errors import NotFound

    try:
        host = CodeHost.query.filter_by(user_id=current_user.id).first()

        if not host:
            return jsonify({"status": "error", "message": "No host found"})

        s = current_app.csm.get(host.service_id)

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
