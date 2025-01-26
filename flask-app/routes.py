
import uuid
from functools import wraps
import docker
from flask import (abort, current_app, render_template, session, request,  g, 
                   redirect, url_for, jsonify)
from flask_login import (current_user, login_required, logout_user)
from jtlutil.flask.flaskapp import insert_query_arg
from app import app, get_db
from db import insert_keystroke_data
from slugify import slugify
from jtlutil.docker.dctl import container_status, create_cs_pair, logger, container_list
from __version__ import version

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
        username=slugify(current_user.primary_email)
        server_hostname = current_app.app_config.HOSTNAME_TEMPLATE.format(username=username)
        client = docker.DockerClient(base_url=current_app.app_config.SSH_URI )
        server_status = container_status(client, current_user.primary_email )
        
        containers = container_list(client) if current_user.is_staff else []
          
    else:
        server_hostname = None
        server_status = None
        containers = []
        
    return render_template("index.html", current_user=current_user,
                           server_hostname=server_hostname, server_status=server_status, form_data=form_data,
                           containers = containers)



@app.route("/start")
@login_required
def start_server():
    
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

@app.route("/telem", methods=["GET", "POST"])
def telem():
    if request.method == "POST":
        telemetry_data = request.get_json()
        current_app.logger.info(f"Telemetry data received: {telemetry_data}")
        # Process telemetry data here if needed
        content_length = request.content_length
        current_app.logger.info(f"Content-Length of telemetry data: {content_length}")
        
        conn = get_db()
        insert_keystroke_data(conn, telemetry_data)
        
        return  jsonify(content_length)