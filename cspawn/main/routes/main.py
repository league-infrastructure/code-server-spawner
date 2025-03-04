""" """

import uuid
from datetime import datetime
from functools import wraps
from typing import cast

from flask import (abort, current_app, flash, jsonify, redirect,
                   render_template, request, session, url_for)
from flask_login import current_user, login_required

from cspawn.__version__ import __version__ as version

from cspawn.main import main_bp
from cspawn.main.models import Class, User, db

from cspawn.util.names import class_code


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

            return render_template("index_instructor.html", **context)

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

        current_app.csm.keyrate.add_report(request.get_json())

    return jsonify("OK")


@main_bp.route("/classes")
@login_required
def classes():
    if not current_user.is_authenticated or not current_user.is_student:
        abort(403)
    taking = current_user.classes_taking
    instructing = current_user.classes_instructing
    return render_template("classes.html", taking=taking, instructing=instructing, **context)


@main_bp.route('/classes/<class_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_class(class_id):
    from cspawn.docker.models import HostImage

    if not current_user.is_instructor:
        return redirect(url_for('main.classes'))

    form_data = request.form

    if class_id == 'new':
        if request.method == 'POST':
            # New class, and we are posting a form with the values.
            print("!!!!!", form_data.get('image_id'))
            image = HostImage.query.get(form_data.get('image_id'))
            if not image:
                flash('Invalid image selected.', 'error')
                return redirect(url_for('main.edit_class', class_id=class_id))

            class_ = Class(
                name=form_data.get('name'),
                description=form_data.get('description'),
                class_code=class_code(),
                image_id=image.id,
                start_date=form_data.get('start_date') or datetime.now().astimezone()
            )

            instructor = User.query.get(current_user.id)

            class_.instructors.append(instructor)
            db.session.add(class_)
        else:
            # New class, first time viewing the form, so it is empty.
            class_ = None

    else:
        # Existing class
        class_ = Class.query.get(class_id)

    if request.method == 'POST':
        # Existing class, posting changes.

        image = HostImage.query.get(form_data.get('image_id'))
        if not image:
            flash('Invalid image selected.', 'error')
            return redirect(url_for('main.edit_class', class_id=class_id))

        class_.name = form_data.get('name') or image.name
        class_.description = form_data.get('description') or image.desc
        class_.start_date = form_data.get('start_date') or class_.start_date
        class_.end_date = form_data.get('end_date') or class_.end_date
        class_.image_id = image.id

        db.session.commit()
        return redirect(url_for('main.classes'))

    all_images = HostImage.query.filter(
        (HostImage.is_public == True) | (HostImage.creator_id == current_user.id)
    ).all()

    return render_template('class_form.html', clazz=class_, all_images=all_images, **context)


@main_bp.route('/classes/<int:class_id>/delete')
@login_required
def delete_class(class_id):
    if not current_user.is_instructor:
        return redirect(url_for('main.classes'))

    class_ = Class.query.get(class_id)
    if class_:
        # Remove all students and instructors from the class
        class_.students.clear()
        class_.instructors.clear()
        db.session.commit()  # Commit the changes to update the relationships

        db.session.delete(class_)
        db.session.commit()
        flash('Class deleted.')
    return redirect(url_for('main.classes'))


@main_bp.route('/classes/add', methods=['POST'])
@login_required
def add_class():
    if not current_user.is_student:
        return redirect(url_for('main.classes'))

    class_code = str(request.form.get('class_code')).strip()
    class_ = Class.query.filter_by(class_code=class_code).first()

    if class_:
        current_user.classes_taking.append(class_)
        db.session.commit()
    else:
        flash('Unknown class code.')

    if current_user.is_instructor or current_user.is_admin:
        return redirect(url_for('main.classes'))
    elif current_user.is_student:
        return redirect(url_for('main.index'))
    else:
        abort(403)


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


@main_bp.route('/class/<int:class_id>/view')
@login_required
def view_class(class_id):
    class_ = Class.query.get_or_404(class_id)
    return render_template('class_view.html', class_=class_, **context)


@main_bp.route("/class/<int:class_id>/start")
@login_required
def start_class(class_id) -> str:
    from cspawn.docker.models import CodeHost, HostImage

    class_ = Class.query.get(class_id)

    if not class_:
        flash("Class not found", "error")
        return redirect(url_for("main.index"))

    image = class_.image

    # Look for an existing CodeHost for the current user
    extant_host = CodeHost.query.filter_by(user_id=current_user.id).first()

    if extant_host:
        flash("A host is already running for the current user", "info")
        return redirect(url_for("hosts.index"))

    # Create a new CodeHost instance
    s = current_app.csm.get_by_username(current_user.username)

    if not s:
        s = current_app.csm.new_cs(
            user=current_user,
            image=image.image_uri,
            repo=image.repo_uri,
            syllabus=image.syllabus_path,
        )

        flash(f"Host {s.name} started successfully", "success")
    else:
        s.sync_to_db()
        flash("Host already running", "info")

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

    s = current_app.csm.get(code_host.service_id)
    if not s:
        flash("Host not found", "danger")
        return redirect(url_for("admin.list_code_hosts"))
    s.stop()
    db.session.delete(code_host)
    db.session.commit()
    flash("Host deleted successfully", "success")
    return redirect(url_for("main.index"))
