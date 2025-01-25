
import uuid
from functools import wraps

from flask import (abort, current_app, render_template, session, request,  g, redirect, url_for)
from flask_login import (current_user, login_required, logout_user)
from jtlutil.flask.flaskapp import insert_query_arg
from app import app
from slugify import slugify


def ensure_session():
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
        current_app.logger.info(f"New session created with ID: {session['session_id']}")
    else:
        pass

@app.before_request
def before_request():
    ensure_session()
    app.load_user(current_app)

@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def staff_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(
            current_user, "is_staff", False
        ):
            current_app.logger.warning(
                f"Unauthorized access attempt by user {current_user.id if current_user.is_authenticated else 'Anonymous'}"
            )
            abort(403)
        return f(*args, **kwargs)

    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(
            current_user, "is_admin", False
        ):
            current_app.logger.warning(
                f"Unauthorized access attempt by user {current_user.id if current_user.is_authenticated else 'Anonymous'}"
            )
            abort(403)
        return f(*args, **kwargs)

    return decorated_function

@app.route("/", methods=["GET", "POST"])
def index():
    
    if request.method == "POST":
        # Handle form submission
        form_data = request.form
        current_app.logger.info(f"Form submitted with data: {form_data}")
        # Process form data here
        action = form_data.get("action")
        
        match form_data.get("action"):
            case "start":
                url = insert_query_arg(url_for('start_server'),"redirect",url_for("index", _external=True))
                app.logger.info("Redirecting to start server: {url}")
                return redirect(url)
            case "create":
                ...
            case "login":
                return redirect(url_for('auth.login', next=url_for('index', _external=True)))
            case "logout":
               return redirect(url_for('auth.logout', next=url_for('index', _external=True)))
        
    else:
        form_data = {}

    if current_user.is_authenticated:
        server_name = current_app.app_config.HOSTNAME_TEMPLATE.format(username=slugify(current_user.primary_email))
    else:
        server_name = None
        
    return render_template("index.html", current_user=current_user, server_name=server_name, form_data=form_data)



@app.route("/start")
@login_required
def start_server():
    import docker
    from jtldockerctl.dctl import create_cs_pair, logger
    import logging
    import requests
    import time
    
    logger.setLevel(logging.DEBUG)
    
    client = docker.DockerClient(base_url=current_app.app_config.SSH_URI )

    nvc, pa = create_cs_pair(client, current_app.app_config, current_app.app_config.IMAGES_PYTHONCS,
                             current_user.primary_email)

    hostname = pa.labels['caddy']

    hostname_url = f"https://{hostname}"
    max_retries = 20
    retry_delay = 2  # seconds

    for _ in range(max_retries):
        try:
            response = requests.get(hostname_url)
            if response.status_code in [200, 302]:
                break
        except requests.exceptions.SSLError:
            current_app.logger.warning(f"SSL error encountered when connecting to {hostname_url}")
        time.sleep(retry_delay)
    else:
        current_app.logger.error(f"Failed to get a valid response from {hostname_url} after {max_retries} attempts")


    return redirect(hostname_url)


@app.route("/private/staff")
@staff_required
def staff():
    return render_template("private-staff.html", current_user = current_user)


@app.route("/private/admin")
@staff_required
def admin():
    return render_template("private-admin.html", current_user = current_user)


@app.route("/public")
def public():
    return render_template("public.html", current_user = current_user)