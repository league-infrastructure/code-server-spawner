"""
Routes for logging in, registering, and managing users.
"""

import uuid
from flask import (current_app, flash, redirect, render_template, request,
                   session, url_for)
from flask_dance.contrib.google import google
from flask_login import current_user, login_required, login_user, logout_user
from oauthlib.oauth2.rfc6749.errors import TokenExpiredError

from cspawn.main.models import User, db
from cspawn.util.app_support import set_role_from_email
from cspawn.util.auth import find_username

from . import auth_bp, logger


def _context():
    """Return the default context for rendering templates."""
    from cspawn.init import default_context  # Breaks circular import

    return default_context


@auth_bp.route("/")
def login_index():
    """Redirect to the login page."""
    return redirect(url_for("auth.login"))


@auth_bp.route("/profile")
def profile():
    """Render the profile page."""
    if current_user.is_authenticated:
        return render_template("profile.html", user=current_user, **_context())
    else:
        return render_template("profile.html", user=None, **_context())


@auth_bp.route("/login")
def login():
    """Render the login page."""
    return render_template("login.html", **_context())


@auth_bp.route("/login/google")
def google_login():
    """Handle Google OAuth login."""
    if not google.authorized:
        return redirect(url_for("google.login"))

    try:
        resp = google.get("/oauth2/v1/userinfo")
    except TokenExpiredError:
        logger.error("Token expired")
        return redirect(url_for("google.login"))

    assert resp.ok

    user_info = resp.json()

    email = user_info.get("email")

    user = User.query.filter_by(email=email).first()
    if user is None:
        user = User(
            username="not_set",
            user_id="google_" + user_info["id"],
            email=email,
            oauth_provider="google",
            oauth_id=user_info["id"],
            avatar_url=user_info["picture"],
        )

        set_role_from_email(current_app, user)
        user.username = find_username(user)

        db.session.add(user)
        db.session.commit()

        user = User.query.filter_by(email=email).first()

    login_user(user)

    return redirect(url_for("main.index"))


@auth_bp.route("/logout")
def logout():
    """Log out the user and revoke the Google OAuth token if authorized."""
    # Revoke the token
    if google.authorized:
        token = google.blueprint.token["access_token"]
        try:
            resp = google.post(
                "https://accounts.google.com/o/oauth2/revoke",
                params={"token": token},
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            if resp.ok:
                logger.info("Token revoked successfully")
            else:
                logger.error("Failed to revoke token")
        except TokenExpiredError:
            logger.error("Token expired")

    # Clear the session
    session.clear()

    # Log out the user
    logout_user()

    return redirect(url_for("main.index"))


@auth_bp.route("/uplogin", methods=["POST", "GET"])
def uplogin():
    """Render the login page for username/password login."""
    if request.method == "POST":
        form = request.form

        username = form.get("username")
        password = form.get("password")

        user = User.query.filter_by(username=username).first()

        if user is None:
            flash("Invalid username or password", "error")
            return render_template("login.html", form=form, **_context())

        if user.password != password:
            flash("Invalid username or password", "error")
            return render_template("login.html", form=form, **_context())

        login_user(user)
        return redirect(url_for("main.index"))
    else:
        form = {}
    return render_template("login.html", **_context())


@auth_bp.route("/register", methods=["POST", "GET"])
def register():
    """Handle user registration."""

    from cspawn.main.models import db, Class

    if request.method == "POST":
        form = request.form

        class_reg = bool(form.get("classreg"))

        if class_reg:
            # Just want to get the class code into the form

            return render_template("register.html", form=form, **_context())

        username = form.get("username")
        password = form.get("password")
        class_code = form.get("class_code").strip()

        user = User.query.filter_by(username=username).first()

        if user:
            flash("Username is taken", "error")
            return render_template("register.html", form=form, **_context())

        class_ = Class.query.filter_by(class_code=class_code).first()

        if not class_:
            flash("Invalid class code", "error")
            return render_template("register.html", form=form, **_context())

        if user is None:
            user = User(
                user_id=str(uuid.uuid4()),
                username=username,
                email=None,
                oauth_provider=None,
                oauth_id=None,
                avatar_url=None,
                is_student=True,
                password=password,
            )

            user.classes_taking.append(class_)

            db.session.add(user)
            db.session.commit()

        flash("User created. You can login", "success")
        login_user(user)
        return redirect(url_for("main.index"))
    else:
        form = {}

    return render_template("register.html", form=form, **_context())


@auth_bp.route("/admin/users")
@login_required
def admin_users():
    """Render the admin users page."""
    users = User.query.all()
    return render_template("admin_users.html", users=users, **_context())


@auth_bp.route("/admin/user/<int:userid>", methods=["GET", "POST"])
@login_required
def admin_user(userid):
    """Handle user management for a specific user."""
    user = User.query.get_or_404(userid)

    if request.method == "POST":
        if "delete" in request.form:
            db.session.delete(user)
            db.session.commit()
            flash("User deleted", "success")
            return redirect(url_for("auth.admin_users"))

        user.username = request.form.get("username")
        user.email = request.form.get("email")
        user.oauth_provider = request.form.get("oauth_provider")
        user.oauth_id = request.form.get("oauth_id")
        user.avatar_url = request.form.get("avatar_url")

        db.session.commit()
        flash("User updated", "success")
        return redirect(url_for("auth.admin_user", userid=userid))

    return render_template("admin_user.html", user=user)
