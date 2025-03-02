from flask_login import UserMixin
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.types import TypeDecorator


import uuid


class GUID(TypeDecorator):
    """Platform-independent GUID type.

    Uses PostgreSQL's UUID type, and stores as string in SQLite.
    """

    import uuid

    impl = String

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(UUID(as_uuid=True))
        else:
            return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return str(value)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return uuid.UUID(value)


class GoogleUser(UserMixin):
    """Represents a user with attributes fetched from Google OAuth."""

    def __init__(self, user_data):
        self.user_data = user_data
        self.id = user_data["id"]
        self.primary_email = user_data["primaryEmail"]
        self.groups = user_data.get("groups", [])
        self.org_unit = user_data.get("orgUnitPath", "")
        self._is_admin = user_data.get("isAdmin", False)

    @property
    def is_league(self):
        """Return true if the user is a League user."""
        return self.primary_email.endswith("@jointheleague.org")

    @property
    def is_student(self):
        """Return true if the user is a student."""
        return self.primary_email.endswith("@students.jointheleague.org")

    @property
    def is_admin(self):
        return self._is_admin and self.is_league

    @property
    def is_staff(self):
        return self.is_league and "staff@jointheleague.org" in self.groups

    @property
    def role(self):
        if self.is_admin:
            return "admin"
        elif self.is_staff:
            return "staff"
        elif self.is_student:
            return "student"
        elif self.is_league:
            return "league"
        else:
            return "Public"

    @property
    def is_public(self):
        return not self.is_league

    def get_full_user_info(self):
        return self.user_data
