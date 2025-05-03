import os
import pytest

# The app and db arguments are injected by Pytest fixtures. See test/conftest.py
# for details.

ADMIN_PAGES = ["/admin/", "/admin/users", "/admin/classes", "/admin/images", "/admin/hosts"]


@pytest.fixture(scope="module")
def admin_user(app, db):
    # Retrieve or create admin user in the database
    from cspawn.models import User

    admin = db.session.query(User).filter_by(user_id="__admin__").first()
    assert admin is not None, "Admin user not found in DB"
    yield admin


class TestAdminCoverage:
    def test_not_production(self, app):
        assert not os.environ.get("PRODUCTION"), "Should not run in production!"
        uri = app.config["SQLALCHEMY_DATABASE_URI"]
        assert "localhost" in uri or uri.startswith("sqlite://"), f"Unexpected DB URI: {uri}"

    def test_admin_pages_accessible(self, client, admin_user):
        # Log in as admin (simulate session or set headers as needed)
        with client.session_transaction() as sess:
            sess["_user_id"] = str(admin_user.id)  # Flask-Login expects string
            sess["_fresh"] = True
            sess["is_admin"] = True
        for url in ADMIN_PAGES:
            resp = client.get(url, follow_redirects=False)
            assert resp.status_code == 200, f"Failed to GET {url}, got {resp.status_code}"
