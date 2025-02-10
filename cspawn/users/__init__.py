from flask import Blueprint

class_bp = Blueprint('classes', __name__, template_folder='templates')

import logging  

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.ERROR)
logger.setLevel(logging.ERROR)
logger.debug("Class blueprint loaded")

from .routes import *