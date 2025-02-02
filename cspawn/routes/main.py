
import json
import logging
import time
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

import docker
import requests
from cspawn.__version__ import __version__ as version
from cspawn.init import CI_FILE, get_db
from cspawn.app import app

from flask import (abort, current_app, g, jsonify, redirect, render_template,
                   request, session, url_for, flash)
from flask_login import current_user, login_required, logout_user
from jtlutil.docker.dctl import ( container_status, logger, make_container_name, get_mapped_port)
from jtlutil.flask.flaskapp import insert_query_arg, is_running_under_gunicorn
from slugify import slugify
from humanize import naturaltime, naturaldelta


context = {
    "version": version,
    "current_user": current_user,
}

def ensure_session():
    
    if "cron" in request.path or "telem" in request.path:
        return
    
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
        current_app.logger.info(f"New session created with ID: {session['session_id']} for {request.path}")
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

empty_status = {
    'containerName': '',
    'containerId': '',
    'state': '',
    'memory_usage': 0,
    'hostname': '',
    'instanceId': '',
    'lastHeartbeat': '',
    'average30m': None,
    'seconds_since_report': 0, 
    'port': None
}



@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("home"))
    else:
        return redirect(url_for("login"))
    
@app.route("/login")
def login():
    
    return render_template("login.html", **context)



def unk_filter(v):

    return v if  v  else "?"


@app.route("/home", methods=["GET", "POST"])
@login_required
def home():
     
    app.jinja_env.filters['unk_filter'] = unk_filter
     
    username=slugify(current_user.primary_email)
    server_hostname = current_app.app_config.HOSTNAME_TEMPLATE.format(username=username)
    client = docker.DockerClient(base_url=current_app.app_config.SSH_URI )
    server_status = container_status(client, current_user.primary_email )
        
    if current_user.is_staff:
        containers = app.csm.containers_list_cached()

        return render_template("admin.html", server_hostname=server_hostname, server_status=server_status,
                            containers = containers, **context)
            
    else:

        containers = []
        return render_template("home.html", server_status=server_status, 
                               server_hostname=server_hostname, 
                            
                               **context)

@app.route("/stop")
@login_required
def stop_server():

    server_id = request.args.get('server_id')
    client = docker.DockerClient(base_url=current_app.app_config.SSH_URI)
    
    if not current_user.is_staff or not server_id:
        
        container_name = make_container_name(current_user.primary_email)
        container = client.containers.get(container_name)        
        vnc_container = client.containers.get(f"{container_name}-novnc")
        
        vnc_container.stop()

        flash("Server stopped successfully", "success")
        return redirect(url_for('home'))

    else:

        s = app.csm.get(server_id)
        s.stop()
        
        flash(f"Server {s.name} stopped successfully", "success")
        return redirect(url_for('home'))
        



@app.route("/start")
@login_required
def start_server():
    # Get query parameters
    hostname = request.args.get('hostname')
    iteration = int(request.args.get('iteration', 0))
    start_time = float(request.args.get('start_time', time.time()))
    
    # If no hostname, this is the initial request
    if not hostname:

        is_devel = not is_running_under_gunicorn()
        
        s = app.csm.new_cs(current_user.primary_email)
        
    
        # Check if server is immediately ready
        time.sleep(1)  # Initial pause
        if check_server_ready(hostname_url):
            assert False #update_container_info(current_app, get_db())
            return redirect(hostname_url)
            
        ctx = {
            'hostname': hostname,
            'hostname_url': hostname_url,
            'start_time': start_time
        }
        # If not ready, redirect to start the polling process
        return redirect(url_for('start_server', iteration=1, **ctx, **context))
    
    else:
        # With parameters, this is a continuation of the polling process
       
        # If we have hostname, we're in the polling phase
        hostname_url = f"https://{hostname}"
        
        # Sleep if this isn't the first iteration
        if iteration >= 1:
            time.sleep(1)
        
        # Check if server is ready
        if check_server_ready(hostname_url):
            return redirect(hostname_url)
        
        # Calculate elapsed time
        elapsed_time = int(time.time() - start_time)
        
        # Maximum wait time (5 minutes)
        if elapsed_time > 300:
            flash("Server startup timed out after 5 minutes", "error")
            return redirect(url_for('home'))
        
        # Render loading page with updated iteration
        ctx = {
            'hostname': hostname,
            'hostname_url': hostname_url,
            'start_time': start_time,
        }
        next_url = url_for('start_server', iteration=iteration + 1, **ctx)
        return render_template('loading.html', iteration=iteration, next_url=next_url, **ctx, **context)

@app.route("/private/staff")
@staff_required
def staff():
    return render_template("private-staff.html", **context)

@app.route("/telem", methods=["GET", "POST"])
def telem():
    if request.method == "POST":
        
        conn = get_db()
        
        telemetry_data = request.get_json()
        
         # fix the containerID to be the same as the containerName
        telemetry_data['containerName'] = telemetry_data.get('containerID')
    
        print (telemetry_data)
    
    return jsonify(telemetry_data )
        
@app.route("/write-test", methods=["GET", "POST"])
def write_test():
    
    from cspawn.db import insert_user_account
    from datetime import datetime   
    from  uuid import uuid4
    
    db = get_db()
    insert_user_account(db, str(uuid4()), 'foobar', datetime.now())
    db.close()
  
    return jsonify("OK")