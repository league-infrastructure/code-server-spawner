import logging

from cspawn.cli.root import cli  # noqa: W0611
from cspawn.models import *  # noqa: W0611

from .config import config  # noqa: W0611
from .db import db  # noqa: W0611
from .fs import fs  # noqa: W0611
from .host import host  # noqa: W0611
from .node import node  # noqa: W0611
from .probe import probe  # noqa: W0611
from .sys import sys  # noqa: W0611
from .telem import telem  # noqa: W0611
from .devel import devel  # noqa: W0611


logger = logging.getLogger("cspawnctl")

if __name__ == "__main__":
    cli(ctx=None, v=None, config_file=None)  # noqa: E1120
