
from cspawn.main import main_bp
from flask import jsonify, request, current_app
from cspawn.telemetry import TelemetryReport, FileStat
from pydantic import ValidationError
from cspawn.models import CodeHost, db


@main_bp.route("/telem", methods=["GET", "POST"])
def telem():
    """Recieve telemetry data and write it into the code host record"""

    if request.method == "POST":

        telemetry_data = request.get_json()

        try:
            telemetry = TelemetryReport(**telemetry_data)

            current_app.mongo.db.telem.insert_one(request.get_json())

            ch = CodeHost.query.filter_by(service_name=telemetry.username).first()

            if ch:
                ch.update_telemetry(telemetry)
                db.session.commit()

        except ValidationError as e:
            return jsonify("Error")

    return jsonify("OK")
