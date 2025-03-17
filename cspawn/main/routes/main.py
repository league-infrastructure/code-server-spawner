""" """

import uuid
from functools import wraps
from typing import cast

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user

from cspawn.__version__ import __version__ as version

from cspawn.main import main_bp
from cspawn.models import Class, db


context = {"version": version, "current_user": current_user}


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


def instructor_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_instructor:
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


def unk_filter(v):
    return v if v else "?"


def datetimeformat(value, format="%Y-%m-%dT%H:%M"):
    return value.strftime(format)


@main_bp.before_app_request
def add_template_filters():
    current_app.jinja_env.filters["unk_filter"] = unk_filter
    current_app.jinja_env.filters["datetimeformat"] = datetimeformat


@main_bp.route("/")
def index():
    from cspawn.models import CodeHost

    if current_user.is_authenticated:
        host = CodeHost.query.filter_by(user_id=current_user.id).first()  # extant code host

        if current_user.is_admin:
            return render_template("index/admin.html", host={}, **context)

        elif current_user.is_instructor:
            if host and host.app_state != "running":
                pass

            all_classes = current_user.classes_instructing
            if all_classes:
                classes = {k: [] for k in ["Running", "Current", "Closed", "Hidden"]}
                for c in all_classes:
                    if c.is_current:
                        if c.running:
                            classes["Running"].append(c)
                        else:
                            classes["Current"].append(c)
                    else:
                        if c.hidden:
                            classes["Hidden"].append(c)
                        else:
                            classes["Closed"].append(c)
            else:
                classes = []

            return render_template(
                "index/instructor.html", host=host, classes=classes, return_url=url_for("main.index"), **context
            )

        elif current_user.is_student:
            return render_template("index/student.html", host=host, return_url=url_for("main.index"), **context)

        else:
            return render_template("index/public.html", **context)

    return redirect(url_for("auth.login"))
