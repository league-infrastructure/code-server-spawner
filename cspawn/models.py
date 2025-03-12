"""
Database Models
"""

from slugify import slugify
from datetime import datetime, timezone
from hashlib import md5
from flask import Flask
from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    func,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, relationship, joinedload, validates
from sqlalchemy_utils import PasswordType, database_exists, create_database
from sqlalchemy import event, create_engine
from tzlocal import get_localzone_name
from dataclasses import dataclass
from .telemetry import TelemetryReport, FileStat


class Base(DeclarativeBase):
    """Base class for all models"""


db = SQLAlchemy(model_class=Base)


class User(UserMixin, db.Model):
    """Main User record"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(200), nullable=False, index=True)
    username = Column(String(50), unique=True, nullable=True)
    email = Column(String(255), unique=True, nullable=True, index=True)
    password = Column(PasswordType(schemes=["bcrypt"]), nullable=True)
    timezone = Column(String(50), nullable=True, default=get_localzone_name())

    # OAuth fields
    oauth_provider = Column(String(50), nullable=True)  # 'google', 'github', 'cleaver'
    oauth_id = Column(String(255), unique=True, nullable=True)  # Provider-specific ID
    avatar_url = Column(String(500), nullable=True)

    is_active = Column(Boolean, default=True, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    is_student = Column(Boolean, default=False, nullable=False)
    is_instructor = Column(Boolean, default=False, nullable=False)

    display_name = Column(String(255), nullable=True)
    birth_year = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=func.now())

    # Add the relationships for classes_instructing and classes_taking
    classes_instructing = relationship(
        "Class", secondary="class_instructors", back_populates="instructors"
    )
    classes_taking = relationship(
        "Class", secondary="class_students", back_populates="students"
    )

    @hybrid_property
    def role(self):
        if self.is_admin:
            return "admin"
        elif self.is_instructor:
            return "instructor"
        elif self.is_student:
            return "student"
        else:
            return "public"

    @hybrid_property
    def code_host(self):
        return self.code_hosts.first()

    @validates("username")
    def _clean_username(this, key, value):
        return User.clean_username(value)

    @classmethod
    def clean_username(cls, username):
        return slugify(username)

    def __repr__(self):
        return (
            f"<User(id={self.id}, username={self.username}, "
            f"email={self.email}, provider={self.oauth_provider})>"
        )

    @classmethod
    def create_root_user(cls, ap: Flask | str):

        if isinstance(ap, str):
            password = ap
        elif isinstance(ap, Flask):
            password = ap.app_config["ADMIN_PASSWORD"]

        existing_user = cls.query.filter_by(id=0).first()
        if existing_user:
            return existing_user

        root_user = cls(
            id=0,
            user_id="__root__",
            username="root",
            password=password,
            is_admin=True,
            is_active=True,
        )
        db.session.add(root_user)
        db.session.commit()
        return root_user

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.username,
            "email": self.email,
            "password": self.password.hash.decode('utf-8') if self.password else None,
            "timezone": self.timezone,
            "oauth_provider": self.oauth_provider,
            "oauth_id": self.oauth_id,
            "avatar_url": self.avatar_url,
            "is_active": self.is_active,
            "is_admin": self.is_admin,
            "is_student": self.is_student,
            "is_instructor": self.is_instructor,
            "display_name": self.display_name,
            "birth_year": self.birth_year,
            "created_at": self.created_at.isoformat()
        }

    @classmethod
    def from_dict(cls, data):
        from sqlalchemy_utils.types.password import Password

        password = data.pop("password", None)

        user = cls(**data)

        if password:
            user.password = Password(password, secret=False)

        user.created_at = datetime.fromisoformat(data["created_at"]) if user.created_at else datetime.now()

        return user


class Class(db.Model):
    """A collections of students and instructors"""

    __tablename__ = "classes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    term = Column(String(255), nullable=True)
    location = Column(String(255), nullable=True)
    timezone = Column(String(255), nullable=True)
    reference = Column(String(255), nullable=True)  # URL or other reference
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=True)
    recurrence_rule = Column(String(255), nullable=True)
    image_id = Column(Integer, ForeignKey("host_images.id"), nullable=False)
    image = relationship("HostImage", back_populates="classes")
    start_script = Column(Text, nullable=True)

    class_code = Column(String(40), nullable=True)

    active = Column(Boolean, default=True, nullable=False)
    hidden = Column(Boolean, default=False, nullable=False)

    instructors = relationship(
        "User", secondary="class_instructors", back_populates="classes_instructing"
    )
    students = relationship(
        "User", secondary="class_students", back_populates="classes_taking"
    )

    @classmethod
    def from_dict(cls, data):
        instructors = data.pop("instructors", [])
        students = data.pop("students", [])

        if data.get("start_date"):
            data["start_date"] = datetime.fromisoformat(data["start_date"])
        if data.get("end_date"):
            data["end_date"] = datetime.fromisoformat(data["end_date"])

        class_instance = cls(**data)

        # Use the session's no_autoflush context manager
        with db.session.no_autoflush:
            if instructors:
                class_instance.instructors = User.query.filter(User.id.in_(instructors)).all()
            if students:
                class_instance.students = User.query.filter(User.id.in_(students)).all()

        return class_instance

    def to_dict(self):
        fields = [
            "id", "name", "description", "term", "location", "timezone", "reference",
            "start_date", "end_date", "recurrence_rule", "image_id", "start_script",
            "class_code", "active", "hidden"
        ]
        if self.start_date:
            self.start_date = self.start_date.isoformat()
        if self.end_date:
            self.end_date = self.end_date.isoformat()

        data = {field: getattr(self, field) for field in fields}
        data["instructors"] = [instructor.id for instructor in self.instructors]
        data["students"] = [student.id for student in self.students]
        return data

    def __repr__(self):
        return f"<Class(id={self.id}, name={self.name})>"


class_instructors = Table(
    "class_instructors",
    db.Model.metadata,
    Column("class_id", Integer, ForeignKey("classes.id"), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
)

class_students = Table(
    "class_students",
    db.Model.metadata,
    Column("class_id", Integer, ForeignKey("classes.id"), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
)


class CodeHost(db.Model):
    __tablename__ = "code_host"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user = relationship("User", backref="code_hosts")

    service_id = Column(String, nullable=False, unique=True)
    service_name = Column(String, nullable=False)
    container_id = Column(String, nullable=True)
    container_name = Column(String, nullable=True)

    state = Column(String, default="unknown", nullable=False)  # Docker state
    app_state = Column(String, default="unknown", nullable=True)  # Application state

    host_image_id = Column(Integer, ForeignKey("host_images.id"), nullable=True)
    host_image = relationship("HostImage", backref="code_hosts")

    class_id = Column(Integer, ForeignKey("classes.id"), nullable=True)
    class_ = relationship("Class", backref="code_hosts")

    node_id = Column(String, nullable=True)
    node_name = Column(String, nullable=True)
    public_url = Column(String, nullable=True)
    password = Column(String, nullable=True)

    memory_usage = Column(Integer, nullable=True)
    last_stats = Column(DateTime, nullable=True)
    last_heartbeat = Column(DateTime, nullable=True)
    last_utilization = Column(DateTime, nullable=True)
    utilization_1 = Column(Float, nullable=True)
    utilization_2 = Column(Float, nullable=True)

    data = Column(Text, nullable=True)
    labels = Column(Text, nullable=True)

    user_activity_rate = Column(Float, default=0.0, nullable=True)
    last_heartbeat_ago = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.now(timezone.utc),
        onupdate=datetime.now(timezone.utc),
        nullable=False,
    )

    @property
    def heart_beat_ago(self):
        try:
            return datetime.now(timezone.utc) - self.last_heartbeat
        except TypeError:
            return None

    @property
    def seconds_since_last_stats(self):
        try:
            return (datetime.now(timezone.utc) - self.last_stats).total_seconds()
        except TypeError:
            return None

    def update_from_ci(self, ci):
        self.service_name = ci["service_name"]
        self.container_id = ci["container_id"]
        self.node_id = ci["node_id"]
        self.state = ci["state"]
        self.public_url = ci["hostname"]

    def update_stats(self, record: dict):

        record["last_heartbeat"] = (
            datetime.now().astimezone().isoformat()
        )  # set this for every record

        if (
            record.get("utilization_1") is not None
            or record.get("utilization_2") is not None
        ):
            record["last_utilization"] = datetime.now().astimezone().isoformat()

        if record["memory_usage"] is not None:
            record["last_stats"] = datetime.now().astimezone().isoformat()

        if record["username"] is None:
            record["username"] = record["labels"].get("jtl.codeserver.username")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "service_id": self.service_id,
            "service_name": self.service_name,
            "container_id": self.container_id,
            "container_name": self.container_name,
            "state": self.state,
            "app_state": self.app_state,
            "host_image_id": self.host_image_id,
            "class_id": self.class_id,
            "node_id": self.node_id,
            "node_name": self.node_name,
            "public_url": self.public_url,
            "password": self.password,
            "memory_usage": self.memory_usage,
            "last_stats": self.last_stats.isoformat() if self.last_stats else None,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "last_utilization": self.last_utilization.isoformat() if self.last_utilization else None,
            "utilization_1": self.utilization_1,
            "utilization_2": self.utilization_2,
            "data": self.data,
            "labels": self.labels,
            "user_activity_rate": self.user_activity_rate,
            "last_heartbeat_ago": self.last_heartbeat_ago.isoformat() if self.last_heartbeat_ago else None,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data):
        data["last_stats"] = datetime.fromisoformat(data["last_stats"]) if data.get("last_stats") else None
        data["last_heartbeat"] = datetime.fromisoformat(data["last_heartbeat"]) if data.get("last_heartbeat") else None
        data["last_utilization"] = datetime.fromisoformat(
            data["last_utilization"]) if data.get("last_utilization") else None
        data["last_heartbeat_ago"] = datetime.fromisoformat(
            data["last_heartbeat_ago"]) if data.get("last_heartbeat_ago") else None
        data["created_at"] = datetime.fromisoformat(data["created_at"]) if data.get(
            "created_at") else datetime.now(timezone.utc)
        data["updated_at"] = datetime.fromisoformat(data["updated_at"]) if data.get(
            "updated_at") else datetime.now(timezone.utc)
        return cls(**data)

    def update_telemetry(self, telemetry: TelemetryReport):

        from datetime import timedelta, datetime

        self.last_stats = telemetry.timestamp
        self.last_heartbeat = telemetry.timestamp
        self.last_utilization = telemetry.timestamp

        self.utilization_1 = telemetry.average30m
        self.utilization_2 = telemetry.keystrokes

        self.user_activity_rate = telemetry.reportingRate

    def __repr__(self):
        return f"<CodeHost(id={self.id}, user_id={self.user_id}, service_id={self.service_id})>"


class HostImage(db.Model):
    __tablename__ = "host_images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    desc = Column(String, nullable=True)
    hash = Column(String, nullable=False)
    image_uri = Column(String, nullable=False)
    repo_uri = Column(String, nullable=True)
    repo_branch = Column(String, nullable=True)
    repo_dir = Column(String, nullable=True)

    syllabus_path = Column(String, nullable=True)

    startup_script = Column(String, nullable=True)

    is_public = Column(Boolean, default=False, nullable=False)

    creator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    creator = relationship("User", backref="host_images")

    created_at = Column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.now(timezone.utc),
        onupdate=datetime.now(timezone.utc),
        nullable=False,
    )

    @staticmethod
    def set_hash(mapper, connection, target):

        def generate_hash(*args):
            hash_input = "".join([str(arg) for arg in args if arg is not None])
            return md5(hash_input.encode("utf-8")).hexdigest()

        target.hash = generate_hash(
            target.image_uri,
            target.repo_uri,
            target.repo_branch,
            target.repo_dir,
            target.syllabus_path,
            target.startup_script,
            target.is_public,
            target.creator_id,
        )

    classes = relationship("Class", back_populates="image")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "desc": self.desc,
            "hash": self.hash,
            "image_uri": self.image_uri,
            "repo_uri": self.repo_uri,
            "repo_branch": self.repo_branch,
            "repo_dir": self.repo_dir,
            "syllabus_path": self.syllabus_path,
            "startup_script": self.startup_script,
            "is_public": self.is_public,
            "creator_id": self.creator_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data):
        data["created_at"] = datetime.fromisoformat(data["created_at"]) if data.get(
            "created_at") else datetime.now(timezone.utc)
        data["updated_at"] = datetime.fromisoformat(data["updated_at"]) if data.get(
            "updated_at") else datetime.now(timezone.utc)
        return cls(**data)


event.listen(HostImage, "before_insert", HostImage.set_hash)
event.listen(HostImage, "before_update", HostImage.set_hash)


def ensure_database_exists(app: Flask):
    uri = app.db.engine.url
    engine = create_engine(uri)
    if not database_exists(engine.url):
        create_database(engine.url)


def export_dict():
    import json

    users = [u.to_dict() for u in User.query.all()]
    classes = [c.to_dict() for c in Class.query.all()]
    images = [i.to_dict() for i in HostImage.query.all()]
    hosts = [h.to_dict() for h in CodeHost.query.all()]

    return {
        "users": users,
        "images": images,
        "classes": classes,
        "hosts": hosts
    }


def import_dict(data):

    db.create_all()

    for user_data in data['users']:
        user = User.from_dict(user_data)
        db.session.add(user)

    db.session.commit()

    for image_data in data['images']:
        image = HostImage.from_dict(image_data)
        db.session.add(image)

    db.session.commit()

    for class_data in data['classes']:
        class_ = Class.from_dict(class_data)
        db.session.add(class_)

    db.session.commit()

    for host_data in data.get('hosts', []):
        host = CodeHost.from_dict(host_data)
        db.session.add(host)

    db.session.commit()
