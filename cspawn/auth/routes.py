from flask import Blueprint, redirect, url_for, request, render_template, session, flash
from flask_login import login_user, current_user, login_required, logout_user
from flask_dance.contrib.google import google
from oauthlib.oauth2.rfc6749.errors import TokenExpiredError, InvalidClientError

from . import auth_bp, logger


def default_context():
    from cspawn.init import default_context # Breaks circular import
    return default_context

@auth_bp.route("/")
def login_index():
    return redirect(url_for("auth.login"))

@auth_bp.route("/profile")
def profile():
   
    if current_user.is_authenticated:
        return render_template("profile.html", user=current_user, **default_context())
    else:
        return render_template("profile.html", user=None, **default_context)

@auth_bp.route("/login")
def login():

    return render_template("login.html", **default_context())

@auth_bp.route("/login/google")
def google_login():
    
    from cspawn.auth.models.user import User, db
    
    if not google.authorized:
        
        return redirect(url_for("google.login"))
    
    resp = google.get("/oauth2/v1/userinfo")

    assert resp.ok
    
    user_info = resp.json()
    
    user = User.query.filter_by(email=user_info["email"]).first()
    if user is None:
        user = User(
            username=None,
            email=user_info.get("email"),
            oauth_provider="google",
            oauth_id=user_info["id"],
            avatar_url=user_info["picture"]
        )
        
        db.session.add(user)
        db.session.commit()
    
        user = User.query.filter_by(email=user_info["email"]).first()
        
    print("XXX", user)
        
    login_user(user)
    
    return redirect(url_for("auth.profile"))

@auth_bp.route("/logout")

def logout():
    # Revoke the token
    if google.authorized:
        token = google.blueprint.token["access_token"]
        resp = google.post(
            "https://accounts.google.com/o/oauth2/revoke",
            params={"token": token},
            headers={"content-type": "application/x-www-form-urlencoded"}
        )
        if resp.ok:
            logger.info("Token revoked successfully")
        else:
            logger.error("Failed to revoke token")
    
    # Clear the session
    session.clear()
    
    # Log out the user
    logout_user()
    return redirect(url_for("auth.login"))

@auth_bp.route("/uplogin", methods=["POST", "GET"])
def uplogin():
  
    return render_template("login.html", **default_context())

@auth_bp.route("/register", methods=["POST", "GET"])
def register():
    
    from cspawn.auth.models.user import User, db
    
    if request.method == "POST":
        form = request.form
        
        username = form.get("username")       
        password = form.get("password")
        
        user = User.query.filter_by(username=username).first()
        if user is None:
            user = User(
                username=username,
                email=None,
                oauth_provider=None,
                oauth_id=None,
                avatar_url=None,
                password=password
            )
   
            db.session.add(user)
            db.session.commit()
        else:
            flash("Username is taken", "error")
            return render_template("register.html", form=form, **default_context())
        
        flash("User created. You can login", "success")
        return redirect(url_for("auth.login"))
    else:
        form = {}
    
    return render_template("register.html", form=form, **default_context())


@auth_bp.route("/admin/users")
@login_required
def admin_users():
    from cspawn.auth.models.user import User, db
    users = User.query.all()
    return render_template("admin_users.html", users=users, **default_context())


@auth_bp.route("/admin/user/<int:userid>", methods=["GET", "POST"])
@login_required
def admin_user(userid):
    from cspawn.auth.models.user import User, db
     
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

