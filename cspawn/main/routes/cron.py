"""These endpoints get called by the cron script. """


from flask import current_app, jsonify
from cspawn.app import app

@app.route("/cron/minutely")
def minutely():
    current_app.logger.info("Minutely cron job")

    current_app.csm.collect_containers()

    return jsonify({"status": "OK"})

@app.route("/cron/hourly")
def hourly():
    current_app.logger.info("Hourly cron job")
    
    return jsonify({"status": "OK"})

@app.route("/cron/daily")
def daily():
    current_app.logger.info("Daily cron job")
    
    return jsonify({"status": "OK"})




