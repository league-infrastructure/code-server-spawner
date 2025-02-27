from flask import Blueprint

users_bp = Blueprint("users", __name__, template_folder="templates")

import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.ERROR)
logger.setLevel(logging.ERROR)
logger.debug("Class blueprint loaded")

from .routes import *
