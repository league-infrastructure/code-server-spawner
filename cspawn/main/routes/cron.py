"""These endpoints get called by the cron script."""

from flask import current_app, jsonify

from cspawn.main import logger, main_bp


@main_bp.route("/cron/minutely")
def minutely():
    current_app.logger.info("Minutely cron job")

    return jsonify({"status": "OK"})


@main_bp.route("/cron/hourly")
def hourly():
    current_app.logger.info("Hourly cron job")

    return jsonify({"status": "OK"})


@main_bp.route("/cron/daily")
def daily():
    current_app.logger.info("Daily cron job")

    return jsonify({"status": "OK"})
