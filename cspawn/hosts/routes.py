import time
import docker

from flask import (current_app, flash, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required


from cspawn.hosts import hosts_bp


@hosts_bp.route("/")
def index():
    return render_template("hosts/index.html")


@hosts_bp.route("/start")
@login_required
def start_server():
    from docker.errors import APIError, NotFound

    # Get query parameters
   
    service_id = request.args.get('service_id')
    iteration = int(request.args.get('iteration', 0))
    start_time = float(request.args.get('start_time', time.time()))
    
    s =  current_app.csm.get_by_username(current_user.primary_email)

    if not s:
        s = current_app.csm.new_cs(current_user.primary_email)
    
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

@hosts_bp.route("/stop")
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

@hosts_bp.route("/service/<service_id>/is_ready", methods=["GET"])
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
