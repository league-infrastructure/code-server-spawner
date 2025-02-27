import logging
from functools import lru_cache
from pathlib import Path

from faker import Faker

from cspawn.config import find_parent_dir
from cspawn.hosts.control import logger as ctrl_logger
from cspawn.init import init_app
from cspawn.main.models import *
from cspawn.util import configure_config_tree

logger = logging.getLogger(__name__)


def create_demo_users(app):
    """Function to load demo data into the database."""
    # Implement your demo data loading logic here
    # Delete all users with email addresses in the 'example.com' domain

    from cspawn.util import set_role_from_email

    faker = Faker()
    users = [
        {
            "user_id": faker.bothify(text="?????"),
            "username": "EricAdmin",
            "email": "eric.busboom@jointheleague.org",
            "password": "password",
        },
        {
            "user_id": faker.bothify(text="?????"),
            "username": "BobStaff",
            "email": "bob.staff@jointheleague.org",
            "password": "password",
        },
        {
            "user_id": faker.bothify(text="?????"),
            "username": "sally",
            "email": "sally.forth@students.jointheleague.org",
            "password": "password",
        },
    ]

    for user in users:
        user = User(**user)
        set_role_from_email(app, user)
        db.session.add(user)

    db.session.commit()

    assert len(User.query.all()) >= 3


def create_demo_images(app):

    host_images = [
        {
            "name": "Ubuntu 20.04 LTS",
            "image_uri": "https://example.com/images/ubuntu-20.04.img",
            "repo_uri": "https://example.com/repos/ubuntu-20.04",
            "startup_script": "#!/bin/bash\napt-get update -y",
            "is_public": True,
        },
        {
            "name": "CentOS 8",
            "image_uri": "https://example.com/images/centos-8.img",
            "repo_uri": "https://example.com/repos/centos-8",
            "startup_script": "#!/bin/bash\nyum update -y",
            "is_public": False,
        },
        {
            "name": "Debian 10",
            "image_uri": "https://example.com/images/debian-10.img",
            "repo_uri": None,
            "startup_script": None,
            "is_public": True,
        },
    ]

    for image in host_images:
        host_image = HostImage(**image)
        db.session.add(host_image)

    db.session.commit()

    assert len(HostImage.query.all()) >= 3


def create_demo_code_hosts(app):
    """
    Create CodeHost records with fake data and associate them with HostImage records.

    :param session: SQLAlchemy Session object.
    :param num_records: Number of CodeHost records to create.
    """
    fake = Faker()
    with app.app_context():
        # Fetch all HostImage records
        host_images = db.session.query(HostImage).all()

        # Fetch all User records
        users = db.session.query(User).all()

        if not host_images:
            print("No HostImage records found. Please ensure they exist before creating CodeHost records.")
            return

        code_hosts = []

        for i in range(5):
            host_image = host_images[i % len(host_images)]  # Cycle through HostImage records
            user = users[i % len(users)]
            code_host = CodeHost(
                service_id=fake.uuid4(),
                user_id=user.id,
                service_name=fake.uuid4(),
                container_id=fake.uuid4(),
                container_name=fake.domain_word(),
                state="unknown",  # Default state
                host_image_id=host_image.id,
            )
            code_hosts.append(code_host)

        db.session.bulk_save_objects(code_hosts)
        db.session.commit()


def make_data(app):

    faker = Faker()

    with app.app_context():

        create_demo_users(app)
        create_demo_images(app)
        create_demo_code_hosts(app)


def load_data(app):

    import json

    import cspawn
    from cspawn.util import set_role_from_email

    data_dir = Path(cspawn.__file__).parent.parent / "data"

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
            image = HostImage(**image_data)
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
    return init_app(config_dir=find_parent_dir(), log_level=log_level)


@lru_cache
def get_logger(ctx):
    log_level = get_logging_level(ctx)

    ctrl_logger.setLevel(log_level)
    logger.setLevel(log_level)
    return logger


@lru_cache
def get_config():

    c = configure_config_tree(find_parent_dir())

    if len(c["__CONFIG_PATH"]) == 0:
        raise Exception("No configuration files found. Maybe you are in the wrong directory?")

    return c
