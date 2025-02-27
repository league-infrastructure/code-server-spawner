from flask import Blueprint

hosts_bp = Blueprint("hosts", __name__, template_folder="templates")

import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.ERROR)
logger.setLevel(logging.ERROR)

from .routes import *
