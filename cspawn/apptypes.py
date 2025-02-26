from cspawn.hosts.control import CodeServerManager
from cspawn.main.models import db


from flask import Flask
from flask_bootstrap import Bootstrap5
from flask_font_awesome import FontAwesome
from flask_pymongo import PyMongo
from flask_sqlalchemy import SQLAlchemy

class App(Flask):
    app_config: dict
    mongodb: PyMongo
    db: SQLAlchemy
    csm: CodeServerManager
    bootstrap: Bootstrap5
    font_awesome: FontAwesome