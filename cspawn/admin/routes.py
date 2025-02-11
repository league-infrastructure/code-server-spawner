import json

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, session, url_for)
from flask_dance.contrib.google import google
from flask_login import current_user, login_required, login_user, logout_user
from oauthlib.oauth2.rfc6749.errors import (InvalidClientError,
                                            TokenExpiredError)

from cspawn.main.models import User, db
from cspawn.util import role_from_email

from . import admin_bp, logger


def default_context():
    from cspawn.init import default_context  # Breaks circular import
    return default_context

@auth_bp.route("/")
def login_index():
    return redirect(url_for("auth.login"))
