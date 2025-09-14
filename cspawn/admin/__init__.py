from flask import Blueprint

admin_bp = Blueprint("admin", __name__, template_folder="templates")

import logging

logger = logging.getLogger(__name__)

logger.debug("Class blueprint loaded")

from .routes import *
