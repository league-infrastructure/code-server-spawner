import json
from datetime import datetime
from typing import cast
from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from cspawn.models import HostImage
from cspawn.models import Class, CodeHost, User, db
from cspawn.util.names import class_code

from . import admin_bp


def _context():
    from cspawn.init import default_context  # Breaks circular import

    return default_context


@admin_bp.route("/")
@login_required
def index():
    return render_template("admin/index.html", **_context())


@admin_bp.route("/hosts")
@login_required
def list_code_hosts():
    code_hosts = CodeHost.query.all()
    return render_template("admin/code_hosts.html", code_hosts=code_hosts)


@admin_bp.route("/host/<int:host_id>/delete", methods=["POST"])
@login_required
def delete_host(host_id):
    code_host = CodeHost.query.get_or_404(host_id)
    db.session.delete(code_host)
    db.session.commit()
    flash("Host deleted successfully", "success")
    return redirect(url_for("admin.list_code_hosts"))


@admin_bp.route("/host/<int:host_id>/stop", methods=["POST"])
@login_required
def stop_host(host_id):
    code_host = CodeHost.query.get(host_id)

    if not host_id:
        flash("No host ID provided", "danger")
        return redirect(url_for("admin.list_code_hosts"))

    code_host = CodeHost.query.get_or_404(host_id)
    s = ca.csm.get(code_host.service_id)
    if not s:
        flash("Host not found", "danger")
        return redirect(url_for("admin.list_code_hosts"))
    s.stop()
    db.session.delete(code_host)
    db.session.commit()
    flash("Host deleted successfully", "success")
    return redirect(url_for("admin.list_code_hosts"))


@admin_bp.route("/host/<int:host_id>/details", methods=["GET"])
@login_required
def view_host(host_id):
    code_host = CodeHost.query.get_or_404(host_id)
    service = ca.csm.get(code_host.service_id)
    if not service:
        flash("Service not found", "danger")
        return redirect(url_for("admin.list_code_hosts"))
    return render_template("admin/view_host.html", code_host=code_host, service=service)


@admin_bp.route("/images")
@login_required
def list_images():
    images = HostImage.query.all()
    image_data = []
    for image in images:
        code_host_count = CodeHost.query.filter_by(host_image_id=image.id).count()
        image_data.append({"image": image, "code_host_count": code_host_count})
    return render_template("admin/images.html", image_data=image_data)


@admin_bp.route("/image/<int:image_id>", methods=["GET", "POST"])
@login_required
def edit_image(image_id):
    image = HostImage.query.get_or_404(image_id)
    has_code_hosts = CodeHost.query.filter_by(host_image_id=image_id).count() > 0
    if request.method == "POST":
        image.name = request.form["name"]
        image.desc = request.form["description"]
        image.image_uri = request.form["image_uri"]
        image.repo_uri = request.form["repo_uri"]
        image.syllabus_path = request.form["syllabus_path"]
        image.is_public = "is_public" in request.form
        db.session.commit()
        flash("Image updated successfully", "success")
        return redirect(url_for("admin.list_images"))
    return render_template(
        "admin/edit_image.html", image=image, has_code_hosts=has_code_hosts
    )


@admin_bp.route("/image/new", methods=["GET", "POST"])
@login_required
def new_image():
    if request.method == "POST":
        new_image = HostImage(
            name=request.form["name"],
            image_uri=request.form["image_uri"],
            repo_uri=request.form["repo_uri"],
            is_public="is_public" in request.form,
            creator_id=current_user.id,
        )
        db.session.add(new_image)
        db.session.commit()
        flash("New image created successfully", "success")
        return redirect(url_for("admin.list_images"))
    return render_template("admin/edit_image.html", image=None, has_code_hosts=False)


@admin_bp.route("/image/<int:image_id>/delete", methods=["POST"])
@login_required
def delete_image(image_id):
    image = HostImage.query.get_or_404(image_id)
    db.session.delete(image)
    db.session.commit()
    flash("Image deleted successfully", "success")
    return redirect(url_for("admin.list_images"))


@admin_bp.route("/images/export", methods=["GET"])
@login_required
def export_images():
    images = HostImage.query.all()
    image_data = [
        {
            "name": image.name,
            "image_uri": image.image_uri,
            "repo_uri": image.repo_uri,
            "is_public": image.is_public,
            "creator_id": image.creator_id,
        }
        for image in images
    ]
    response = current_app.response_class(
        response=json.dumps(image_data),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment;filename=images.json"},
    )
    return response


@admin_bp.route("/images/import", methods=["GET", "POST"])
@login_required
def import_images():
    if request.method == "POST":
        if "file" not in request.files:
            flash("No file part", "danger")
            return redirect(url_for("admin.import_images"))

        file = request.files["file"]
        if file.filename == "":
            flash("No selected file", "danger")
            return redirect(url_for("admin.import_images"))

        if file and file.filename.endswith(".json"):
            try:
                image_data = json.load(file)
                for image in image_data:
                    if not HostImage.query.filter_by(
                        image_uri=image["image_uri"],
                        repo_uri=image["repo_uri"],
                        creator_id=image["creator_id"],
                    ).first():
                        new_image = HostImage(
                            name=image["name"],
                            image_uri=image["image_uri"],
                            repo_uri=image["repo_uri"],
                            is_public=image["is_public"],
                            creator_id=image["creator_id"],
                        )
                        db.session.add(new_image)
                db.session.commit()
                flash("Images imported successfully", "success")
            except Exception as e:
                db.session.rollback()
                flash(f"An error occurred: {str(e)}", "danger")
        else:
            flash("Invalid file format", "danger")

        return redirect(url_for("admin.list_images"))

    return render_template("admin/import_images.html")


@admin_bp.route("/users")
@login_required
def list_users():
    users = User.query.all()
    current_year = datetime.now().year
    return render_template("admin/users.html", users=users, current_year=current_year)


@admin_bp.route("/classes")
@login_required
def classes():
    classes = Class.query.all()
    return render_template("admin/classes.html", classes=classes)


@admin_bp.route("/classes/export", methods=["GET"])
@login_required
def export_classes():
    classes = Class.query.all()
    class_data = [
        {
            "name": class_.name,
            "description": class_.description,
            "class_code": class_.class_code,
            "image_id": class_.image_id,
            "start_date": class_.start_date.isoformat(),
        }
        for class_ in classes
    ]
    response = current_app.response_class(
        response=json.dumps(class_data),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment;filename=classes.json"},
    )
    return response


@admin_bp.route('/classes/<class_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_class(class_id):
    from cspawn.models import HostImage

    if not current_user.is_instructor:
        return redirect(url_for('admin.classes'))

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
            return redirect(url_for('admin.edit_class', class_id=class_id))

        class_.name = form_data.get('name') or image.name
        class_.description = form_data.get('description') or image.desc
        class_.start_date = form_data.get('start_date') or class_.start_date
        class_.end_date = form_data.get('end_date') or class_.end_date
        class_.image_id = image.id

        db.session.commit()
        return redirect(url_for('admin.classes'))

    all_images = HostImage.query.filter(
        (HostImage.is_public == True) | (HostImage.creator_id == current_user.id)
    ).all()

    return render_template('class_form.html', clazz=class_, all_images=all_images, **context)


@admin_bp.route('/classes/<int:class_id>/delete')
@login_required
def delete_class(class_id):
    if not current_user.is_instructor:
        return redirect(url_for('admin.classes'))

    class_ = Class.query.get(class_id)
    if class_:
        # Remove all students and instructors from the class
        class_.students.clear()
        class_.instructors.clear()
        db.session.commit()  # Commit the changes to update the relationships

        db.session.delete(class_)
        db.session.commit()
        flash('Class deleted.')

    return redirect(url_for('admin.classes'))
