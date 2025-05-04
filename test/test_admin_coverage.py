import os
import pytest
from faker import Faker

# The app and db arguments are injected by Pytest fixtures. See test/conftest.py
# for details.

fake = Faker()

ADMIN_PAGES = ["/admin/", "/admin/users", "/admin/classes", "/admin/images", "/admin/hosts"]


@pytest.fixture()
def admin_user(app, db, client):
    # Retrieve or create admin user in the database
    from cspawn.models import User

    admin = db.session.query(User).filter_by(user_id="__admin__").first()
    assert admin is not None, "Admin user not found in DB"

    # Set up admin session
    with client.session_transaction() as sess:
        sess["_user_id"] = str(admin.id)  # Flask-Login expects string
        sess["_fresh"] = True
        sess["is_admin"] = True

    yield admin


class TestAdminCoverage:
    def test_not_production(self, app):
        assert not os.environ.get("PRODUCTION"), "Should not run in production!"
        uri = app.config["SQLALCHEMY_DATABASE_URI"]
        assert "localhost" in uri or uri.startswith("sqlite://"), f"Unexpected DB URI: {uri}"

    def test_admin_pages_accessible(self, client, admin_user):
        # Admin session is already set up by the fixture
        for url in ADMIN_PAGES:
            resp = client.get(url, follow_redirects=False)
            assert resp.status_code == 200, f"Failed to GET {url}, got {resp.status_code}"

    def test_create_class(self, client, admin_user):
        class_data = {
            "name": fake.word(),
            "description": fake.sentence(),
            "class_code": fake.word() + " " + fake.word() + " " + fake.word(),
            "location": fake.city(),
            "term": fake.month_name(),
            "proto_id": "1",
            "start_date": "",  # fake.date_time_this_year(after_now=True).strftime("%Y-%m-%dT%H:%M"),
            "end_date": "",
            "active": "y",
            "public": "y",
            "action": "save",
        }

        # POST to /classes/new/edit
        resp = client.post("/classes/new/edit", data=class_data, follow_redirects=True)
        assert resp.status_code == 200

    def test_start_class(self, client, admin_user, app):
        # Step 1: Call the classes_list route to get JSON list of classes
        resp = client.get("/classes/list")
        assert resp.status_code == 200

        classes_data = resp.json
        assert "instructing" in classes_data, "No instructing classes found in response"

        app.logger.debug(
            f"Found {len(classes_data['instructing'])} classes in instructing list: {[e['name'] for e in classes_data['instructing']]}"
        )

        # Find a class that's not running
        non_running_class = None
        for class_ in classes_data["instructing"]:
            if not class_["running"]:
                non_running_class = class_
                break

        app.logger.debug(f"Non-running class found: {non_running_class}")

        # Stop all running classes
        running_classes = [class_ for class_ in classes_data["instructing"] if class_["running"]]
        for running_class in running_classes:
            class_id = running_class["id"]
            app.logger.debug(f"Stopping running class: {running_class['name']} (ID: {class_id})")
            stop_response = client.post(f"/classes/{class_id}/state?state=stopped")
            assert stop_response.status_code == 200

        # Refresh the class list after stopping all
        resp = client.get("/classes/list")
        classes_data = resp.json

        # Find a non-running class to work with
        non_running_class = None
        for class_ in classes_data["instructing"]:
            if not class_["running"]:
                non_running_class = class_
                break

        # Make sure we have a class to work with
        assert non_running_class is not None, "No classes found to test with"

        # Step 2: Start the class
        class_id = non_running_class["id"]
        start_response = client.post(f"/classes/{class_id}/state?state=running")
        assert start_response.status_code == 200

        # Verify the class is now running
        resp = client.get("/classes/list")
        classes_data = resp.json

        # Find our class in the updated list
        for class_ in classes_data["instructing"]:
            if class_["id"] == class_id:
                assert class_["running"] == True, "Class was not started successfully"
                break
