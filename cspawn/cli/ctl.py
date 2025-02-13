import logging

from cspawn.cli.root import cli

from cspawn.main.models import *

logging.basicConfig(level=logging.ERROR)

logger = logging.getLogger("cspawnctl")

from .root import cli
from .config import config
from .db import db
from .fs import fs
from .host import host
from .sys import sys
from .node import node
from .probe import probe
from .telem import telem

if __name__ == '__main__':
    cli()
