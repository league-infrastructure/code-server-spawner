
from cspawn.main import main_bp
from flask import jsonify, request, current_app
from cspawn.telemetry import TelemetryReport, FileStat
from pydantic import ValidationError


@main_bp.route("/telem", methods=["GET", "POST"])
def telem():
    if request.method == "POST":

        telemetry_data = request.get_json()

        try:
            telemetry = TelemetryReport(**telemetry_data)

            # print(telemetry)

            print("30m", telemetry.average30m)
            print(" 5m", telemetry.average5m)

            current_app.mongo.db.telem.insert_one(request.get_json())
        except ValidationError as e:
            print(e)
            return jsonify("Error")

    return jsonify("OK")
