"""These endpoints get called by the cron script. """


from flask import current_app, jsonify
from cspawn.app import app, get_db, CI_FILE
from cspawn.db import update_container_metrics, update_container_state, join_container_info, update_container_info
from jtlutil.docker.dctl import container_state
import docker 
import os
import json
from pathlib import Path

@app.route("/cron/minutely")
def minutely():
    current_app.logger.info("Minutely cron job")
    db = get_db()
    update_container_info(current_app, get_db)
    return jsonify({"status": "OK"})

@app.route("/cron/hourly")
def hourly():
    current_app.logger.info("Hourly cron job")
    
    return jsonify({"status": "OK"})

@app.route("/cron/daily")
def daily():
    current_app.logger.info("Daily cron job")
    
    return jsonify({"status": "OK"})

