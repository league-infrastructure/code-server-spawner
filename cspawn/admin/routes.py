import json
from datetime import datetime

from flask import (current_app, flash, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_required

from cspawn.main.models import Class, CodeHost, HostImage, User, db

from . import admin_bp


def default_context():
    from cspawn.init import default_context  # Breaks circular import

    return default_context


@admin_bp.route("/")
@login_required
def index():
    return render_template("admin/index.html", **default_context())


@admin_bp.route("/classes")
@login_required
def list_classes():
    classes = Class.query.all()
    return render_template("admin/classes.html", classes=classes)


@admin_bp.route("/code_hosts")
@login_required
def list_code_hosts():
    code_hosts = CodeHost.query.all()
    return render_template("admin/code_hosts.html", code_hosts=code_hosts)


@admin_bp.route("/delete_host", methods=["POST"])
@login_required
def delete_host():
    host_id = request.form.get("host_id")
    if not host_id:
        flash("No host ID provided", "danger")
        return redirect(url_for("admin.list_code_hosts"))

    code_host = CodeHost.query.get_or_404(host_id)
    db.session.delete(code_host)
    db.session.commit()
    flash("Host deleted successfully", "success")
    return redirect(url_for("admin.list_code_hosts"))


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
        image.image_uri = request.form["image_uri"]
        image.repo_uri = request.form["repo_uri"]
        image.syllabus_path = request.form["syllabus_path"]

        image.is_public = "is_public" in request.form
        db.session.commit()
        flash("Image updated successfully", "success")
        return redirect(url_for("admin.list_images"))
    return render_template("admin/edit_image.html", image=image, has_code_hosts=has_code_hosts)


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
                        image_uri=image["image_uri"], repo_uri=image["repo_uri"], creator_id=image["creator_id"]
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
