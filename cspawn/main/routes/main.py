
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

from cspawn.main import main_bp, logger

from flask import (abort, current_app, g, jsonify, redirect, render_template,
                   request, session, url_for, flash)
from flask_login import current_user, login_required, logout_user
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

@main_bp.before_request
def before_request():
    ensure_session()
    
    #app.load_user(current_app)



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

def unk_filter(v):
    return v if  v  else "?"

@main_bp.before_app_request
def add_template_filters():
    current_app.jinja_env.filters['unk_filter'] = unk_filter


@main_bp.route("/")
def index():



    if  current_user.is_authenticated:
        
        if current_user.is_admin:
            
            return render_template("index_admin.html", host={},  **context)
        
        elif current_user.is_instructor:
            
            return render_template("index_instructor.html", **context)
        
        elif current_user.is_student:
            
            return render_template("index_student.html", **context)
       
    
    return render_template("index.html", **context)
        

@main_bp.route("/stop")
@login_required
def stop_server():

    server_id = request.args.get('server_id')
    client = docker.DockerClient(base_url=current_app.app_config.DOCKER_URI)
    
    if not current_user.is_staff or not server_id:
        
        s =  app.csm.get_by_username(current_user.primary_email)
        
        if not s:
            flash("No server found to stop", "error")
            return redirect(url_for('home'))
        else:
            s.stop()

        flash("Server stopped successfully", "success")
        return redirect(url_for('home'))

    elif  current_user.is_staff and server_id:

        s = app.csm.get(server_id)
        s.stop()
        
        flash(f"Server {s.name} stopped successfully", "success")
        return redirect(url_for('home'))
    
    else:
        flash("Server stop disallowed", "error")
        return redirect(url_for('home'))
        
@main_bp.route("/start")
@login_required
def start_server():
    from docker.errors import NotFound, APIError
    # Get query parameters
   
    service_id = request.args.get('service_id')
    iteration = int(request.args.get('iteration', 0))
    start_time = float(request.args.get('start_time', time.time()))
    
    s =  app.csm.get_by_username(current_user.primary_email)

    if not s:
        s = app.csm.new_cs(current_user.primary_email)
    
    if s is None:
        flash("Error starting server", "error")
        return redirect(url_for('home'))
    
    if s.is_ready():
        return redirect(s.hostname_url)
    
    # Calculate elapsed time
    elapsed_time = int(time.time() - start_time)
    
    # Maximum wait time 
    if elapsed_time > 120:
        flash("Server startup timed out", "error")
        return redirect(url_for('home'))
    
    # Render loading page with updated iteration
    ctx = {
        'service_id': s.id,
        'hostname': s.hostname,
        'hostname_url': s.hostname_url,
        'start_time': start_time,
        'host': s,
    }
    next_url = url_for('start_server', iteration=iteration + 1, )
    return render_template('loading.html', iteration=iteration, next_url=next_url, **ctx, **context)

@main_bp.route("/service/<service_id>/is_ready", methods=["GET"])
@login_required
def server_is_ready(service_id):
    from docker.errors import NotFound 
    
    try:
        s = app.csm.get(service_id)
        if s.is_ready():
            return jsonify({"status": "ready", "hostname_url": s.hostname_url})
        else:
            return jsonify({"status": "not_ready"})
    except NotFound as e:
        return jsonify({"status": "error", "message": str(e)})

@main_bp.route("/private/staff")
@staff_required
def staff():
    return render_template("private-staff.html", **context)

@main_bp.route("/telem", methods=["GET", "POST"])
def telem():
    if request.method == "POST":

        
        current_app.csm.keyrate.add_report(request.get_json())
    
    return jsonify("OK")
        