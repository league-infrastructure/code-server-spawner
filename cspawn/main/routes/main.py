""" """

import uuid
from functools import wraps
from typing import cast

from flask import (abort, current_app, flash, jsonify, redirect,
                   render_template, request, session, url_for)
from flask_login import current_user, login_required

from cspawn.__version__ import __version__ as version

from cspawn.main import main_bp
from cspawn.main.models import Class, db


context = {
    "version": version,
    "current_user": current_user,
}


def ensure_session():

    if "cron" in request.path or "telem" in request.path:
        return

    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
        current_app.logger.info(f"New session created with ID: {session['session_id']} for {request.path}")
    else:
        pass


@main_bp.before_request
def before_request():
    ensure_session()

    # app.load_user(current_app)


def staff_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(current_user, "is_staff", False):
            current_app.logger.warning(
                f"Unauthorized access attempt by user {current_user.id if current_user.is_authenticated else 'Anonymous'}"
            )
            abort(403)
        return f(*args, **kwargs)

    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(current_user, "is_admin", False):
            current_app.logger.warning(
                f"Unauthorized access attempt by user {current_user.id if current_user.is_authenticated else 'Anonymous'}"
            )
            abort(403)
        return f(*args, **kwargs)

    return decorated_function


empty_status = {
    "containerName": "",
    "containerId": "",
    "state": "",
    "memory_usage": 0,
    "hostname": "",
    "instanceId": "",
    "lastHeartbeat": "",
    "average30m": None,
    "seconds_since_report": 0,
    "port": None,
}


def unk_filter(v):
    return v if v else "?"


def datetimeformat(value, format='%Y-%m-%dT%H:%M'):
    return value.strftime(format)


@main_bp.before_app_request
def add_template_filters():
    current_app.jinja_env.filters["unk_filter"] = unk_filter
    current_app.jinja_env.filters["datetimeformat"] = datetimeformat


@main_bp.route("/")
def index():
    from cspawn.docker.models import CodeHost

    if current_user.is_authenticated:

        if current_user.is_admin:

            return render_template("index_admin.html", host={}, **context)

        elif current_user.is_instructor:

            classes = current_user.classes_instructing

            return render_template("index_instructor.html", classes=classes, **context)

        elif current_user.is_student:

            host = CodeHost.query.filter_by(user_id=current_user.id).first()  # extant code host

            return render_template("index_student.html", host=host,  image=None, **context)

        else:

            return render_template("index_public.html", **context)

    return render_template("index.html", **context)


@main_bp.route("/private/staff")
@staff_required
def staff():
    return render_template("private-staff.html", **context)


@main_bp.route("/telem", methods=["GET", "POST"])
def telem():
    if request.method == "POST":
        pass
        # current_app.csm.keyrate.add_report(request.get_json())

    return jsonify("OK")


@main_bp.route("/public/promote", methods=["POST"])
def promote():
    """Promote a public user to a student."""
    class_code = request.form.get("class_code")
    class_ = Class.query.filter_by(class_code=class_code).first()

    if class_:
        current_user.is_student = True

        current_user.classes_taking.append(class_)

        db.session.commit()
        flash("You have been promoted to a student.", "success")
    else:
        flash("Invalid class code.", "error")

    return redirect(url_for("main.index"))


@main_bp.route("/host/<int:host_id>/stop", methods=["GET"])
@login_required
def stop_host(host_id):
    from cspawn.docker.models import CodeHost

    code_host = CodeHost.query.get(host_id)

    if not host_id:
        flash("No host ID provided", "danger")
        return redirect(url_for("admin.list_code_hosts"))

    code_host = CodeHost.query.get(host_id)

    try:
        s = current_app.csm.get(code_host.service_id)
    except KeyError:
        s = None

    if not s:
        flash("Host not found", "danger")
        return redirect(url_for("admin.list_code_hosts"))
    s.stop()
    db.session.delete(code_host)
    db.session.commit()
    flash("Host deleted successfully", "success")
    return redirect(url_for("main.index"))
