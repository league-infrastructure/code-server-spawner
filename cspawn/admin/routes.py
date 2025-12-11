import json
from datetime import datetime
from functools import wraps
from operator import is_

from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from cspawn.init import cast_app
from cspawn.models import Class, CodeHost, ClassProto, User, db

from . import admin_bp

ca = cast_app(current_app)


def _context():
    from cspawn.init import default_context  # Breaks circular import

    return default_context


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            if current_user.is_admin:
                return f(*args, **kwargs)

        except AttributeError:
            # Handle the case where current_user does not have is_admin attribute,
            # which may happen if the user is not authenticated
            pass

        return redirect(url_for("main.index"))

    return decorated_function


@admin_bp.route("/")
@admin_required
def index():
    # Gather dashboard stats
    num_code_hosts = CodeHost.query.count()
    num_running_hosts = CodeHost.query.filter_by(state="running").count()
    num_users = User.query.count()
    num_classes = Class.query.count()
    context = _context()
    context.update(
        {
            "num_code_hosts": num_code_hosts,
            "num_running_hosts": num_running_hosts,
            "num_users": num_users,
            "num_classes": num_classes,
        }
    )
    return render_template("admin/index.html", **context)


@admin_bp.route("/hosts")
@admin_required
def list_code_hosts():
    code_hosts = CodeHost.query.all()

    return render_template("admin/code_hosts.html", code_hosts=code_hosts)


@admin_bp.route("/host/<int:host_id>/delete", methods=["POST"])
@admin_required
def delete_host(host_id):
    code_host = CodeHost.query.get_or_404(host_id)
    db.session.delete(code_host)
    db.session.commit()
    flash("Host deleted successfully", "success")
    return redirect(url_for("admin.list_code_hosts"))


@admin_bp.route("/host/<int:host_id>/stop", methods=["POST"])
@admin_required
def stop_host(host_id):
    code_host = CodeHost.query.get(host_id)

    if not host_id:
        flash("No host ID provided", "danger")
        return redirect(url_for("admin.list_code_hosts"))

    code_host = CodeHost.query.get_or_404(host_id)
    s = ca.csm.get(code_host.service_id)
    if not s:
        flash("Host not found", "danger")
        db.session.delete(code_host)
        db.session.commit()
        return redirect(url_for("admin.list_code_hosts"))
    
    s.stop()
    db.session.delete(code_host)
    db.session.commit()
    flash("Host deleted successfully", "success")
    return redirect(url_for("admin.list_code_hosts"))


@admin_bp.route("/host/<int:host_id>/details", methods=["GET"])
@admin_required
def view_host(host_id):
    code_host = CodeHost.query.get_or_404(host_id)
    service = ca.csm.get(code_host.service_id)
    if not service:
        flash("Service not found", "danger")
        return redirect(url_for("admin.list_code_hosts"))
    return render_template("admin/view_host.html", code_host=code_host, service=service)


@admin_bp.route("/protos")
@admin_required
def list_protos():
    protos = []
    for proto in ClassProto.query.all():
        code_host_count = CodeHost.query.filter_by(proto_id=proto.id).count()
        protos.append({"proto": proto, "code_host_count": code_host_count})
    return render_template("admin/protos.html", protos=protos)


@admin_bp.route("/proto/<int:proto_id>", methods=["GET", "POST"])
@admin_required
def edit_proto(proto_id):
    proto = ClassProto.query.get_or_404(proto_id)
    has_code_hosts = CodeHost.query.filter_by(proto_id=proto_id).count() > 0
    if request.method == "POST":
        proto.name = request.form["name"]
        proto.desc = request.form["description"]
        proto.image_uri = request.form["image_uri"]
        proto.repo_uri = request.form["repo_uri"]
        proto.syllabus_path = request.form["syllabus_path"]
        proto.is_public = "is_public" in request.form
        db.session.commit()
        flash("Proto updated successfully", "success")
        return redirect(url_for("admin.list_protos"))
    return render_template("admin/edit_proto.html", proto=proto, has_code_hosts=has_code_hosts)


@admin_bp.route("/proto/new", methods=["GET", "POST"])
@admin_required
def new_proto():
    if request.method == "POST":
        new_proto = ClassProto(
            name=request.form["name"],
            image_uri=request.form["image_uri"],
            repo_uri=request.form["repo_uri"],
            is_public="is_public" in request.form,
            creator_id=current_user.id,
        )
        db.session.add(new_proto)
        db.session.commit()
        flash("New proto created successfully", "success")
        return redirect(url_for("admin.list_protos"))
    return render_template("admin/edit_proto.html", proto=None, has_code_hosts=False)


@admin_bp.route("/proto/<int:proto_id>/delete", methods=["POST"])
@admin_required
def delete_proto(proto_id):
    proto = ClassProto.query.get_or_404(proto_id)
    db.session.delete(proto)
    db.session.commit()
    flash("Proto deleted successfully", "success")
    return redirect(url_for("admin.list_protos"))


@admin_bp.route("/protos/export", methods=["GET"])
@admin_required
def export_protos():
    protos = ClassProto.query.all()
    proto_data = [
        {
            "name": proto.name,
            "image_uri": proto.image_uri,
            "repo_uri": proto.repo_uri,
            "is_public": proto.is_public,
            "creator_id": proto.creator_id,
        }
        for proto in protos
    ]
    response = current_app.response_class(
        response=json.dumps(proto_data),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment;filename=protos.json"},
    )
    return response


@admin_bp.route("/protos/import", methods=["GET", "POST"])
@admin_required
def import_protos():
    if request.method == "POST":
        if "file" not in request.files:
            flash("No file part", "danger")
            return redirect(url_for("admin.import_protos"))

        file = request.files["file"]
        if file.filename == "":
            flash("No selected file", "danger")
            return redirect(url_for("admin.import_protos"))

        if file and file.filename.endswith(".json"):
            try:
                proto_data = json.load(file)
                for proto in proto_data:
                    if not ClassProto.query.filter_by(
                        image_uri=proto["image_uri"], repo_uri=proto["repo_uri"], creator_id=proto["creator_id"]
                    ).first():
                        new_proto = ClassProto(
                            name=proto["name"],
                            image_uri=proto["image_uri"],
                            repo_uri=proto["repo_uri"],
                            is_public=proto["is_public"],
                            creator_id=proto["creator_id"],
                        )
                        db.session.add(new_proto)
                db.session.commit()
                flash("Protos imported successfully", "success")
            except Exception as e:
                db.session.rollback()
                flash(f"An error occurred: {str(e)}", "danger")
        else:
            flash("Invalid file format", "danger")

        return redirect(url_for("admin.list_protos"))

    return render_template("admin/import_protos.html")


@admin_bp.route("/users")
@admin_required
def list_users():
    users = User.query.all()
    current_year = datetime.now().year
    return render_template("admin/users.html", users=users, current_year=current_year)


@admin_bp.route("/users/<int:user_id>/impersonate", methods=["POST"])
@admin_required
def impersonate_user(user_id: int):
    if current_user.id == user_id:
        flash("You're already signed in as that user.", "info")
        return redirect(url_for("admin.list_users"))

    target_user = User.query.get_or_404(user_id)

    if not session.get("impersonator_id"):
        session["impersonator_id"] = current_user.id
        session["impersonator_name"] = (
            current_user.display_name or current_user.username or current_user.email or "admin"
        )

    login_user(target_user)
    flash(
        f"Now impersonating {target_user.display_name or target_user.username or target_user.email}",
        "success",
    )
    return redirect(url_for("main.index"))


@admin_bp.route("/users/stop-impersonating", methods=["POST"])
@login_required
def stop_impersonating():
    impersonator_id = session.pop("impersonator_id", None)
    impersonator_name = session.pop("impersonator_name", None)

    if not impersonator_id:
        flash("You're not impersonating another user.", "info")
        return redirect(url_for("main.index"))

    original_user = User.query.get(impersonator_id)
    if not original_user:
        flash("Original admin account could not be restored. Please log in again.", "danger")
        logout_user()
        return redirect(url_for("auth.logout"))

    login_user(original_user)
    flash(
        f"Returned to {impersonator_name or original_user.display_name or original_user.username or 'admin'}.",
        "success",
    )
    return redirect(url_for("admin.list_users"))


@admin_bp.route("/classes")
@admin_required
def classes():
    classes = Class.query.all()
    return render_template("admin/classes.html", classes=classes)


@admin_bp.route("/classes/export", methods=["GET"])
@admin_required
def export_classes():
    classes = Class.query.all()
    class_data = [
        {
            "name": class_.name,
            "description": class_.description,
            "class_code": class_.class_code,
            "proto_id": class_.image_id,
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


@admin_bp.route("/classes/<class_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_class(class_id):
    from cspawn.main.routes.classes import _edit_class

    return _edit_class(class_id, "admin.classes")


@admin_bp.route("/classes/<int:class_id>/delete")
@admin_required
def delete_class(class_id):
    if not current_user.is_instructor:
        return redirect(url_for("admin.classes"))

    class_ = Class.query.get(class_id)
    if class_:
        # Remove all students and instructors from the class
        class_.students.clear()
        class_.instructors.clear()
        db.session.commit()  # Commit the changes to update the relationships

        db.session.delete(class_)
        db.session.commit()
        flash("Class deleted.")

    return redirect(url_for("admin.classes"))
