import logging
from functools import lru_cache
from pathlib import Path

import click
from faker import Faker

from cspawn.cs_docker.csmanager import logger as ctrl_logger
from cspawn.init import init_app
from cspawn.models import CodeHost, ClassProto, User, db
from cspawn.util.app_support import configure_config_tree
from cspawn.util.config import find_parent_dir

logger = logging.getLogger(__name__)


def create_demo_users(app):
    """Function to load demo data into the database."""
    # Implement your demo data loading logic here
    # Delete all users with email addresses in the 'example.com' domain

    from cspawn.util.app_support import set_role_from_email

    User.create_root_user(app)

    faker = Faker()
    users = [
        {
            "user_id": faker.bothify(text="?????"),
            "username": "BobStaff",
            "email": "bob.staff@jointheleague.org",
            "password": "password",
        },
        {
            "user_id": faker.bothify(text="?????"),
            "username": "sally_f",
            "email": "sally.forth@students.jointheleague.org",
            "password": "password",
        },
    ]

    for user in users:
        user = User(**user)
        existing_user = User.query.filter_by(email=user.email).first()
        if existing_user:
            logger.info(f"User {user.email} already exists. Skipping.")
            continue

        set_role_from_email(app, user)

        db.session.add(user)

    db.session.commit()

    assert len(User.query.all()) >= 3


def create_demo_protos(app):
    host_protos = [
        {
            "name": "Python Apprentice",
            "image_uri": "ghcr.io/league-infrastructure/league-infrastructure/docker-codeserver-python:v0.5.4",
            "repo_uri": "https://github.com/league-curriculum/Python-Apprentice",
            "is_public": True,
            "creator_id": 1,
        },
        {
            "name": "Python Games",
            "image_uri": "ghcr.io/league-infrastructure/league-infrastructure/docker-codeserver-python:latest",
            "repo_uri": "https://github.com/league-curriculum/Python-Games",
            "is_public": True,
            "creator_id": 1,
        },
    ]

    for proto in host_protos:
        host_proto = ClassProto(**proto)

        ClassProto.set_hash(None, None, host_proto)

        existing_proto = ClassProto.query.filter_by(hash=host_proto.hash).first()
        if existing_proto:
            logger.info(f"ClassProto with hash {host_proto.hash} already exists. Skipping.")
            continue

        db.session.add(host_proto)

    db.session.commit()

    assert len(ClassProto.query.all()) >= 2


def create_demo_code_hosts(app):
    """
    Create CodeHost records with fake data and associate them with ClassProto records.

    :param session: SQLAlchemy Session object.
    :param num_records: Number of CodeHost records to create.
    """
    fake = Faker()
    with app.app_context():
        # Fetch all ClassProto records
        host_images = db.session.query(ClassProto).all()

        # Fetch all User records
        users = db.session.query(User).all()

        if not host_images:
            print("No ClassProto records found. Please ensure they exist before creating CodeHost records.")
            return

        code_hosts = []

        for i in range(5):
            host_image = host_images[i % len(host_images)]  # Cycle through ClassProto records
            user = users[i % len(users)]
            code_host = CodeHost(
                service_id=fake.uuid4(),
                user_id=user.id,
                service_name=fake.uuid4(),
                container_id=fake.uuid4(),
                container_name=fake.domain_word(),
                state="unknown",  # Default state
                proto_id=host_image.id,
            )
            code_hosts.append(code_host)

        db.session.bulk_save_objects(code_hosts)
        db.session.commit()


def make_data(app):
    pass


def load_data(app):
    import json

    import cspawn
    from cspawn.util.app_support import set_role_from_email

    data_dir = Path(cspawn.__file__).parent.parent / "data"

    User.create_root_user(app)

    users_file = data_dir / "users.json"

    if users_file.exists():
        with open(users_file, "r") as f:
            users_data = json.load(f)

        for user_data in users_data:
            if "is_admin" in user_data and user_data["is_admin"]:
                user_data["password"] = app.app_config["ADMIN_PASSWORD"]
            user = User(**user_data)
            set_role_from_email(app, user)
            db.session.add(user)

        db.session.commit()

    images_file = data_dir / "images.json"

    if images_file.exists():
        with open(images_file, "r") as f:
            images_data = json.load(f)

        for image_data in images_data:
            image = ClassProto(**image_data)
            db.session.add(image)

        db.session.commit()


def get_logging_level(ctx):
    v = ctx.obj["v"]

    log_level = None
    if v == 0:
        log_level = logging.ERROR
    if v == 1:
        log_level = logging.INFO
    elif v >= 2:
        log_level = logging.DEBUG
    else:
        log_level = logging.ERROR

    return log_level


@lru_cache
def get_app(ctx):
    log_level = get_logging_level(ctx)

    return init_app(config_dir=find_parent_dir(), log_level=log_level, deployment=ctx.obj["deploy"])


@lru_cache
def get_logger(ctx):
    log_level = get_logging_level(ctx)

    ctrl_logger.setLevel(log_level)
    logger.setLevel(log_level)
    return logger


@click.pass_context
def get_config(ctx):
    deploy = ctx.obj["deploy"]

    c = configure_config_tree(find_parent_dir(), deploy=deploy)

    if len(c["__CONFIG_PATH"]) == 0:
        raise Exception("No configuration files found. Maybe you are in the wrong directory?")

    return c
