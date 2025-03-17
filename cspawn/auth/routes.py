"""
Routes for logging in, registering, and managing users.
"""

import uuid
from flask import current_app, flash, redirect, render_template, request, session, url_for
from flask_dance.contrib.google import google
from flask_login import current_user, login_required, login_user, logout_user
from oauthlib.oauth2.rfc6749.errors import TokenExpiredError

from cspawn.models import User, Class, db
from cspawn.util.app_support import set_role_from_email
from cspawn.util.auth import find_username

from . import auth_bp, logger
from .forms import UPRegistrationForm, GoogleRegistrationForm, LoginForm


def _context():
    """Return the default context for rendering templates."""
    from cspawn.init import default_context  # Breaks circular import

    return default_context


@auth_bp.route("/")
def login_index():
    """Redirect to the login page."""
    return google_login()


@auth_bp.route("/login", methods=["POST", "GET"])
def login():
    """Render the login page."""

    form = LoginForm()

    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        login_user(user)
        return redirect(url_for("main.index"))
    else:
        print("!!!", form.errors)
        if form.password.errors:
            class_ = Class.query.filter_by(class_code=form.password.data).first()
            if class_:
                flash("The password matches a class code. Please use a different password.", "error")
            else:
                flash("Invalid username or password.", "error")

        return render_template("login.html", form=form, **_context())


@auth_bp.route("/login/google", methods=["POST", "GET"])
def google_login():
    """Handle Google OAuth login."""

    # The first time we come here, we aren't authorized, so we kick it to
    # the oath blueprint.
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
    user_id = "google_" + user_info["id"]

    user = User.query.filter_by(user_id=user_id).first()
    if user is None:
        user = User(
            username="not_set",  # wll set later, after User constructed.
            user_id="google_" + user_info["id"],
            email=user_info.get("email"),
            oauth_provider="google",
            avatar_url=user_info["picture"],
        )

        set_role_from_email(current_app, user)
        user.username = find_username(user)

    if session.get("reg_class_code"):
        class_code = session["reg_class_code"]
        class_ = Class.query.filter_by(class_code=class_code).first()
        user.classes_taking.append(class_)
        del session["reg_class_code"]

    db.session.add(user)
    db.session.commit()

    user = User.query.filter_by(email=email).first()

    login_user(user)

    return redirect(url_for("main.index"))


def register_user_up(username, password, class_code):
    """Register a user with username and password."""
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

    class_ = Class.query.filter_by(class_code=class_code).first()
    user.classes_taking.append(class_)

    db.session.add(user)
    db.session.commit()

    login_user(user)

    return redirect(url_for("main.index"))


@auth_bp.route("/register")
def register():
    """Handle user registration."""
    return register_google()


@auth_bp.route("/register/google", methods=["POST", "GET"])
def register_google():
    """Handle user registration."""
    form = GoogleRegistrationForm()

    if form.validate_on_submit():
        session["reg_class_code"] = form.class_code.data.strip()
        return redirect(url_for("google.login"))

    return render_template("register_google.html", form=form, **_context())


@auth_bp.route("/register/up", methods=["POST", "GET"])
def register_up():
    """Handle user registration."""
    form = UPRegistrationForm()
    if form.validate_on_submit():
        return register_user_up(form.username.data, form.password.data, form.class_code.data)
    return render_template("register_up.html", form=form, **_context())


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
            return redirect(url_for("auth.admin_users"))

        user.username = request.form.get("username")
        user.email = request.form.get("email")
        user.oauth_provider = request.form.get("oauth_provider")
        user.oauth_id = request.form.get("oauth_id")
        user.avatar_url = request.form.get("avatar_url")

        db.session.commit()
        return redirect(url_for("auth.admin_user", userid=userid))

    return render_template("admin_user.html", user=user)


@auth_bp.route("/profile")
def profile():
    """Render the profile page."""
    if current_user.is_authenticated:
        return render_template("profile.html", user=current_user, **_context())
    else:
        return render_template("profile.html", user=None, **_context())


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
