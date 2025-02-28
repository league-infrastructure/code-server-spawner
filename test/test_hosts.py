from pathlib import Path

import pytest
from faker import Faker

from cspawn.docker.models import CodeHost
from ..cspawn.docker.models import CodeHost
from cspawn.hosts.models import FileStat, KeystrokeReport
from cspawn.init import db
from cspawn.main.models import User
from cspawn.users.models import Role

from .fixtures import *


def test_host_basic(app):

    print(app.app_config["SECRET_KEY"])


def test_create_code_host(app, fake):

    with app.app_context():
        # Generate fake data for CodeHost

        # Create a fake user and student role
        role = Role(name="student")
        db.session.add(role)
        db.session.commit()

        user = User(
            username=fake.user_name(),
            email=fake.email(),
            password=fake.password(),
            role_id=role.id,
        )
        db.session.add(user)
        db.session.commit()

        # Update the CodeHost to be attached to the student role
        code_host.role_id = role.id

        code_host = CodeHost(
            role_id=role.id,
            service_id=fake.uuid4(),
            service_name=fake.word(),
            container_id=fake.uuid4(),
            container_name=fake.word(),
        )
        db.session.add(code_host)
        db.session.commit()

        # Verify that the CodeHost object was created
        assert code_host.id is not None
        assert code_host.service_id is not None
        assert code_host.service_name is not None
        assert code_host.container_id is not None
        assert code_host.container_name is not None

        # Generate fake data for KeystrokeReport
        keystroke_report_data = {
            "timestamp": fake.iso8601(),
            "instance_id": fake.uuid4(),
            "keystrokes": fake.random_int(min=0, max=1000),
            "average30m": fake.random_float(min=0, max=100),
            "reporting_rate": fake.random_int(min=0, max=100),
            "file_stats": [
                {
                    "keystrokes": fake.random_int(min=0, max=100),
                    "last_modified": fake.iso8601(),
                },
                {
                    "keystrokes": fake.random_int(min=0, max=100),
                    "last_modified": fake.iso8601(),
                },
            ],
        }

        KeystrokeReport.add_ks_report(code_host.id, keystroke_report_data)

        # Verify that the KeystrokeReport object was created
        keystroke_report = KeystrokeReport.query.filter_by(
            code_host_id=code_host.id
        ).first()
        assert keystroke_report is not None
        assert keystroke_report.timestamp is not None
        assert keystroke_report.instance_id is not None
        assert keystroke_report.keystrokes is not None
        assert keystroke_report.average30m is not None
        assert keystroke_report.reporting_rate is not None

        # Verify that the FileStat objects were created
        file_stats = FileStat.query.filter_by(
            keystroke_report_id=keystroke_report.id
        ).all()
        assert len(file_stats) == 2
        for file_stat in file_stats:
            assert file_stat.keystrokes is not None
            assert file_stat.last_modified is not None


def test_add_hosts(app, faker):
    pass
