from flask import Blueprint

auth_bp = Blueprint('auth', __name__, template_folder='templates')

import logging  

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.ERROR)
logger.setLevel(logging.DEBUG)
logger.debug("Auth blueprint loaded")

from .routes import *