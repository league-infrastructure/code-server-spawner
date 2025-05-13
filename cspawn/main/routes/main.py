""" """

import uuid
from functools import wraps

from flask import abort, current_app, redirect, render_template, request, session, url_for
from flask_login import current_user

from cspawn.__version__ import __version__ as version

from cspawn.main import main_bp


context = {"version": version, "current_user": current_user}




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

        if current_user.is_instructor or current_user.is_admin:
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

            if current_user.is_admin:
                page = "index/admin.html"
            else:
                page = "index/instructor.html"

            return render_template(page, host=host, classes=classes, return_url=url_for("main.index"), **context)

        elif current_user.is_student:
            return render_template("index/student.html", host=host, return_url=url_for("main.index"), **context)

        else:
            return render_template("index/public.html", **context)

    return redirect(url_for("auth.login"))
