from datetime import datetime
from cspawn.main import main_bp
from cspawn.main.models import Class, User, db


from flask import abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from cspawn.main.routes.main import context
from cspawn.util.names import class_code


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


@main_bp.route('/class/<int:class_id>/view')
@login_required
def view_class(class_id):
    class_ = Class.query.get_or_404(class_id)
    return render_template('class_view.html', class_=class_, **context)


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


@main_bp.route("/classes")
@login_required
def classes():
    if not current_user.is_authenticated:
        return redirect(url_for('main.index'))

    taking = current_user.classes_taking
    instructing = current_user.classes_instructing
    return render_template("classes.html", taking=taking, instructing=instructing, **context)
