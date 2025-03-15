import logging

from flask import Blueprint


main_bp = Blueprint(
    "main",
    __name__,
    static_folder="static",
    static_url_path="/static/main/",
    template_folder="templates",
)


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.ERROR)
logger.setLevel(logging.ERROR)

from .routes import *  # noqa
