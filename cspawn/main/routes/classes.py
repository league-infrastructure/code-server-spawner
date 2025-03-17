from datetime import datetime, timedelta, timezone
import re


from sqlalchemy import UniqueConstraint
from sqlalchemy.exc import IntegrityError
from cspawn.main import main_bp
from cspawn.models import Class, CodeHost, User, db
from cspawn.forms import ClassForm

from flask import abort, current_app, flash, redirect, render_template, request, url_for, jsonify
from flask_login import current_user, login_required

from cspawn.main.routes.main import context, instructor_required
from cspawn.util.names import class_code

from sqlalchemy.orm import joinedload
from cspawn.init import cast_app
from psycopg2.errors import UniqueViolation

ca = cast_app(current_app)


@main_bp.route("/classes/add", methods=["POST"])
@login_required
def add_class():
    if not current_user.is_student:
        return redirect(url_for("main.classes"))

    class_code = str(request.form.get("class_code")).strip()
    class_ = Class.query.filter_by(class_code=class_code).first()

    if class_:
        current_user.classes_taking.append(class_)
        db.session.commit()
    else:
        flash("Unknown class code.", "error")

    if current_user.is_instructor or current_user.is_admin:
        return redirect(url_for("main.classes"))
    elif current_user.is_student:
        return redirect(url_for("main.index"))
    else:
        abort(403)


@main_bp.route("/class/<int:class_id>/start")
@login_required
def start_class(class_id) -> str:
    return_url = request.args.get("return_url", url_for("main.index"))

    class_ = Class.query.get(class_id)

    if not class_:
        flash("Class not found", "error")
        return redirect(return_url)

    image = class_.image

    assert class_.image_id == image.id

    # Look for an existing CodeHost for the current user
    extant_host = CodeHost.query.filter_by(user_id=current_user.id).first()

    if extant_host:
        flash("A host is already running for the current user", "info")
        return redirect(url_for("hosts.index"))

    # Create a new CodeHost instance
    s = ca.csm.get_by_username(current_user.username)

    if not s:
        s, ch = ca.csm.new_cs(user=current_user, image=image, class_=class_)

        if s:
            flash(f"Host {s.name} started successfully", "success")
        else:
            flash("Failed to start host", "error")

        if ch and ch.class_id is None:
            assert ch.host_image_id == image.id
            ch.class_id = class_id
            db.session.add(ch)
            db.session.commit()

    else:
        s.sync_to_db(check_ready=True)
        flash("Host already running", "info")

    return redirect(return_url)


@main_bp.route("/class/<int:class_id>/show")
@login_required
def show_class(class_id):
    class_ = Class.query.get_or_404(class_id)
    return render_template("classes/show.html", class_=class_, **context)


def host_buttons(user, class_):
    from cspawn.util.host import host_class_state, which_host_buttons

    return which_host_buttons(host_class_state(current_user, class_))


context["host_buttons"] = host_buttons


@main_bp.route("/class/<int:class_id>/details")
@login_required
def detail_class(class_id):
    class_ = Class.query.get_or_404(class_id)
    host = CodeHost.query.filter_by(user_id=current_user.id).first()  # extant code host

    print("Host buttons:", host_buttons)

    return render_template(
        "classes/detail.html",
        class_=class_,
        host=host,
        return_url=url_for("main.detail_class", class_id=class_id),
        **context,
    )


@main_bp.route("/classes/<int:class_id>/delete")
@login_required
def delete_class(class_id):
    if not current_user.is_instructor:
        return redirect(url_for("main.classes"))

    class_ = Class.query.get(class_id)

    if class_.students:
        flash("Cannot delete a class with enrolled students.", "error")
        return redirect(url_for("main.index"))

    if class_:
        # Remove all students and instructors from the class
        for student in class_.students:
            host = CodeHost.query.filter_by(user_id=student.id).first()
            if host:
                ca.csm.stop_cs(host.name)
                db.session.delete(host)

        class_.students.clear()
        class_.instructors.clear()
        db.session.commit()  # Commit the changes to update the relationships

        db.session.delete(class_)
        db.session.commit()
        flash("Class deleted.")

    return redirect(url_for("main.index"))


@main_bp.route("/classes/<class_id>/edit", methods=["GET", "POST"])
@instructor_required
def edit_class(class_id):
    return _edit_class(class_id, "main.detail_class")


def _edit_class(class_id, return_page):
    from cspawn.models import HostImage

    ctx = context.copy()
    ctx["return_page"] = return_page

    if not current_user.is_instructor:
        return redirect(url_for(return_page))

    action = request.form.get("action", None)

    if action == "delete":
        return delete_class(class_id)
    elif action == "cancel":
        return redirect(url_for(return_page, class_id=class_id))

    if class_id == "new":
        form = ClassForm()
        class_ = Class()
        if request.method == "POST":
            instructor = User.query.get(current_user.id)
            class_.instructors.append(instructor)

    else:
        # Editing existing
        class_ = Class.query.options(joinedload(Class.instructors)).get(class_id)
        if request.method == "GET":
            form = ClassForm.from_model(class_)
        else:
            form = ClassForm()

    all_images = HostImage.query.filter(HostImage.is_public | (HostImage.creator_id == current_user.id)).all()

    form.image_id.choices = [(0, "")] + [(image.id, image.name) for image in all_images]

    if request.method == "POST":
        reload = not form.name.data

        form.to_model(class_, current_user)

        if form.validate():
            db.session.add(class_)
            try:
                db.session.commit()
            except (UniqueViolation, IntegrityError) as e:
                db.session.rollback()
                # Guess it is b/c of the class code
                flash("Class code must be unique", "error")
                form.class_code.errors.append("Class code must be unique. Generated a new one.")
                form.class_code.data = class_code()

                return render_template("classes/edit.html", clazz=class_, form=form, **ctx)
            if reload:
                reload_form = ClassForm.from_model(class_)
                reload_form.image_id.choices = form.image_id.choices
                return render_template("classes/edit.html", clazz=class_, form=reload_form, **ctx)
            else:
                return redirect(url_for(return_page, class_id=class_.id))
        else:
            ca.logger.info("Form did not validate: %s", form.errors)
            flash(f"Form errors: {','.join(form.errors)}", "error")

    return render_template("classes/edit.html", clazz=class_, form=form, **ctx)


@main_bp.route("/classes/<class_id>/copy", methods=["GET"])
@instructor_required
def copy_class(class_id):
    class_ = Class.query.get_or_404(class_id)

    clone = Class(**{c.name: getattr(class_, c.name) for c in class_.__table__.columns if c.name != "id"})

    clone.name = f"{clone.name} (copy)"
    clone.class_code = class_code()
    clone.students = []
    clone.instructors = [current_user]
    clone.hidden = False
    clone.running = False
    clone.running_at = None
    clone.stops_at = None

    db.session.add(clone)
    db.session.commit()

    return redirect(url_for("main.edit_class", class_id=clone.id))


@main_bp.route("/classes/students/remove", methods=["POST"])
@login_required
def remove_students():
    data = request.get_json()
    student_ids = data.get("student_ids", [])
    class_id = data.get("class_id")

    class_ = Class.query.get_or_404(class_id)

    if not current_user.is_instructor or current_user not in class_.instructors:
        return jsonify({"error": "Unauthorized access"}), 403

    for student_id in student_ids:
        student = User.query.get(student_id)

        if student in class_.students:
            host = CodeHost.query.filter_by(service_name=student.username).first()
            if host:
                ca.csm.stop_cs(host.service_name)
                db.session.delete(host)

            class_.students.remove(student)

    db.session.commit()
    return jsonify({"success": "Selected students have been removed from the class."})


@main_bp.route("/classes/<int:class_id>/state", methods=["POST"])
@login_required
def class_run_state(class_id):
    state = request.args.get("state")
    class_ = Class.query.get_or_404(class_id)

    if state == "running":
        if class_.start_date and datetime.now(timezone.utc) < class_.start_date:
            flash("Can't start class before start date", "error")
            return jsonify({"error": "Can't start class before start date"}), 400
        elif class_.end_date and datetime.now(timezone.utc) > class_.end_date:
            flash("Can't start class after end date", "error")
            return jsonify({"error": "Can't start class after end date"}), 400

        class_.running = True
        class_.running_at = datetime.now(timezone.utc)
        class_.stops_at = class_.running_at + timedelta(hours=3)
    elif state == "stopped":
        class_.running = False
        class_.running_at = None
        class_.stops_at = None
    else:
        return jsonify({"error": "Invalid state"}), 400

    db.session.commit()
    return jsonify({"success": "Class state updated"}), 200
