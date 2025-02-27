"""

"""
import uuid
from functools import wraps

from flask import (abort, current_app, jsonify, render_template, request,
                   session)
from flask_login import current_user

from cspawn.__version__ import __version__ as version
from cspawn.main import main_bp

context = {
    "version": version,
    "current_user": current_user,
}


def ensure_session():

    if "cron" in request.path or "telem" in request.path:
        return

    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
        current_app.logger.info(
            f"New session created with ID: {session['session_id']} for {request.path}"
        )
    else:
        pass


@main_bp.before_request
def before_request():
    ensure_session()

    # app.load_user(current_app)


def staff_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(
            current_user, "is_staff", False
        ):
            current_app.logger.warning(
                f"Unauthorized access attempt by user {current_user.id if current_user.is_authenticated else 'Anonymous'}"
            )
            abort(403)
        return f(*args, **kwargs)

    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(
            current_user, "is_admin", False
        ):
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


@main_bp.before_app_request
def add_template_filters():
    current_app.jinja_env.filters["unk_filter"] = unk_filter


@main_bp.route("/")
def index():

    if current_user.is_authenticated:

        if current_user.is_admin:

            return render_template("index_admin.html", host={}, **context)

        elif current_user.is_instructor:

            return render_template("index_instructor.html", **context)

        elif current_user.is_student:

            return render_template("index_student.html", **context)

    return render_template("index.html", **context)


@main_bp.route("/private/staff")
@staff_required
def staff():
    return render_template("private-staff.html", **context)


@main_bp.route("/telem", methods=["GET", "POST"])
def telem():
    if request.method == "POST":

        current_app.csm.keyrate.add_report(request.get_json())

    return jsonify("OK")
