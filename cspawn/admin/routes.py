import json

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, session, url_for)
from flask_dance.contrib.google import google
from flask_login import current_user, login_required, login_user, logout_user
from oauthlib.oauth2.rfc6749.errors import (InvalidClientError,
                                            TokenExpiredError)

from cspawn.main.models import User, HostImage, db
from cspawn.util import role_from_email

from . import admin_bp, logger


def default_context():
    from cspawn.init import default_context  # Breaks circular import
    return default_context

@admin_bp.route("/")
@login_required
def index():
    return render_template("admin/index.html", **default_context())

@admin_bp.route("/images")
@login_required
def list_images():
    images = HostImage.query.all()
    return render_template("images.html", images=images)

@admin_bp.route("/image/<int:image_id>", methods=["GET", "POST"])
@login_required
def edit_image(image_id):
    image = HostImage.query.get_or_404(image_id)
    if request.method == "POST":
        image.name = request.form["name"]
        image.image_uri = request.form["image_uri"]
        image.repo_uri = request.form["repo_uri"]
        image.is_public = "is_public" in request.form
        db.session.commit()
        flash("Image updated successfully", "success")
        return redirect(url_for("admin.list_images"))
    return render_template("edit_image.html", image=image)

@admin_bp.route("/image/new", methods=["GET", "POST"])
@login_required
def new_image():
    if request.method == "POST":
        new_image = HostImage(
            name=request.form["name"],
            image_uri=request.form["image_uri"],
            repo_uri=request.form["repo_uri"],
            is_public="is_public" in request.form,
            creator_id=current_user.id
        )
        db.session.add(new_image)
        db.session.commit()
        flash("New image created successfully", "success")
        return redirect(url_for("admin.list_images"))
    return render_template("edit_image.html", image=None)

@admin_bp.route("/image/<int:image_id>/delete", methods=["POST"])
@login_required
def delete_image(image_id):
    image = HostImage.query.get_or_404(image_id)
    db.session.delete(image)
    db.session.commit()
    flash("Image deleted successfully", "success")
    return redirect(url_for("admin.list_images"))


