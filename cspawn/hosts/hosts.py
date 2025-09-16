import time
from typing import cast

import docker
from flask import current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from cspawn.util.apptypes import App
from cspawn.models import ClassProto
from cspawn.hosts import hosts_bp
from cspawn.models import CodeHost, db
from cspawn.cs_docker.csmanager import CSMService

import json

ca = cast(App, current_app)

raise NotImplementedError("Maybe not used anymore")
