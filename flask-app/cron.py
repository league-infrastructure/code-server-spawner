"""These endpoints get called by the cron script. """


from flask import current_app, jsonify
from app import app, get_db, CI_FILE
from db import update_container_metrics, update_container_state, join_container_info
from jtlutil.docker.dctl import container_state
import docker 
import os
import json
from pathlib import Path

@app.route("/cron/minutely")
def minutely():
    current_app.logger.info("Minutely cron job")
    
    db = get_db()
    update_container_metrics(db)
    
    client = docker.DockerClient(base_url=current_app.app_config.SSH_URI )
    update_container_state(db,container_state(client))
    
    d = join_container_info(db)
    
    (Path(current_app.app_config.DATA_DIR) / CI_FILE).write_text(json.dumps(d))

    db.close()
    
    return jsonify({"status": "OK"})

@app.route("/cron/hourly")
def hourly():
    current_app.logger.info("Hourly cron job")
    
    return jsonify({"status": "OK"})

@app.route("/cron/daily")
def daily():
    current_app.logger.info("Daily cron job")
    
    return jsonify({"status": "OK"})

