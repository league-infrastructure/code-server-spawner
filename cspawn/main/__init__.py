
import logging
from typing import cast
from flask import Blueprint, current_app
from cspawn.util.apptypes import App

main_bp = Blueprint(
    "main", __name__, static_folder="static", static_url_path="/static/main/", template_folder="templates"
)

ca = cast(App, current_app)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.ERROR)
logger.setLevel(logging.ERROR)

from .routes import *  # noqa
