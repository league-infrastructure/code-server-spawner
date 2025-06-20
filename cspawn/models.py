"""
Database Models
"""

from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import md5

from flask import Flask
from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from slugify import slugify
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
    create_engine,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, relationship, validates
from sqlalchemy_utils import PasswordType, create_database, database_exists

from tzlocal import get_localzone_name

from .telemetry import TelemetryReport


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
    is_anonymous = Column(Boolean, default=False, nullable=False)

    display_name = Column(String(255), nullable=True)
    birth_year = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=func.now())

    # Add the relationships for classes_instructing and classes_taking
    classes_instructing = relationship("Class", secondary="class_instructors", back_populates="instructors")
    classes_taking = relationship("Class", secondary="class_students", back_populates="students")

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
        return f"<User(id={self.id}, username={self.username}, email={self.email}, provider={self.oauth_provider})>"

    @classmethod
    def create_root_user(cls, ap: Flask | str):
        if isinstance(ap, str):
            password = ap
        elif isinstance(ap, Flask):
            password = ap.app_config["ADMIN_PASSWORD"]

        existing_user = cls.query.filter_by(id=0).first()
        if existing_user:
            return existing_user

        root_user = cls(id=0, user_id="__root__", username="root", password=password, is_admin=True, is_active=True)
        db.session.add(root_user)
        db.session.commit()
        return root_user

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "username": self.username,
            "email": self.email,
            "password": self.password.hash.decode("utf-8") if self.password else None,
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
            "created_at": self.created_at.isoformat(),
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
    start_date = Column(DateTime(timezone=True), nullable=False)  # Time and date that the class begins.
    end_date = Column(DateTime(timezone=True), nullable=True)
    recurrence_rule = Column(String(255), nullable=True)
    proto_id = Column(Integer, ForeignKey("class_proto.id"), nullable=False)
    proto = relationship("ClassProto", back_populates="classes")
    start_script = Column(Text, nullable=True)

    class_code = Column(String(40), nullable=True, unique=True)

    active = Column(Boolean, default=True, nullable=False)  # Can the class be started ( running )?
    hidden = Column(Boolean, default=False, nullable=False)  # Is the class shown to students?
    public = Column(Boolean, default=False, nullable=True)  # Is the class shown to other instructors?

    running = Column(Boolean, default=False, nullable=False)
    running_at = Column(DateTime, nullable=True)  # Time the class began allowing students to join.
    stops_at = Column(DateTime, nullable=True)  # Time the class ends and students are no longer allowed to join.

    instructors = relationship("User", secondary="class_instructors", back_populates="classes_instructing")
    students = relationship("User", secondary="class_students", back_populates="classes_taking")

    data = Column(JSON, nullable=True)  # JSON data for class configuration

    def update(self):
        """Update markers and flags for the class"""
        t = datetime.now(timezone.utc)

        if t < self.start_date or t > self.end_date:
            self.active = False
            self.running = False
            self.running_at = None
            self.stops_at = None
        else:
            self.active = True

    @hybrid_property
    def can_start(self) -> bool:
        """Can the class be started?"""

        now = datetime.now(timezone.utc)
        return (
            now >= self.start_date
            and (self.end_date is None or now <= self.end_date)
            and self.active
            and not self.running
        )

    @hybrid_property
    def can_register(self) -> bool:
        """Can students register for the class?"""

        now = datetime.now(timezone.utc)
        return now >= self.start_date and (self.end_date is None or now <= self.end_date) and self.active

    @hybrid_property
    def is_current(self) -> bool:
        """Is the class open for students to join?"""

        now = datetime.now(timezone.utc)

        return (
            self.active
            and not self.hidden
            and now >= self.start_date
            and (self.end_date is None or now <= self.end_date)
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

    def host_class_state(self, user: User, host: "CodeHost") -> str:
        """Return the state of the host for the given class. Use which_host_buttons
        to turn this state into a list of buttons to display to the user."""

        if not host:
            if self.running:
                return "stopped"  # There is no host running
            else:
                return "waiting"  # Waiting for class to start

        elif host and self.id == host.class_id:
            if host.app_state == "ready":
                # There is a host running, and it is for this class
                return "running"
            else:
                return "starting"
        else:
            # There is a host running, but it is not for this class"
            return "other"

    def to_dict(self):
        fields = [
            "id",
            "name",
            "description",
            "term",
            "location",
            "timezone",
            "reference",
            "start_date",
            "end_date",
            "recurrence_rule",
            "proto_id",
            "start_script",
            "class_code",
            "active",
            "hidden",
            "running",
            "running_at",
            "stops_at",
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


class HostState(Enum):
    UNKNOWN = "unknown"
    RUNNING = "running"
    READY = "ready"
    MIA = "mia"
    STARTING = "starting"


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

    proto_id = Column(Integer, ForeignKey("class_proto.id"), nullable=True)
    class_proto = relationship("ClassProto", backref="code_hosts")

    class_id = Column(Integer, ForeignKey("classes.id"), nullable=True)
    class_ = relationship("Class", backref="code_hosts")

    node_id = Column(String, nullable=True)
    node_name = Column(String, nullable=True)
    public_url = Column(String, nullable=True)
    password = Column(String, nullable=True)

    memory_usage = Column(Integer, nullable=True)
    last_stats = Column(DateTime, nullable=True)
    last_heartbeat = Column(DateTime, nullable=True)  # Last time of any report
    last_utilization = Column(DateTime, nullable=True)  # Last time user editied a file
    user_activity_rate = Column(Float, default=0.0, nullable=True)  # 5 M keystroke rate.
    utilization_1 = Column(Float, nullable=True)
    utilization_2 = Column(Float, nullable=True)

    data = Column(Text, nullable=True)
    labels = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc), nullable=False
    )

    @staticmethod
    def to_minutes(td: timedelta):
        return int(round(td.total_seconds() / 60))

    @hybrid_property
    def heart_beat_ago(self) -> int:
        """Time since last hearbeat in minutes"""
        try:
            return CodeHost.to_minutes(datetime.now(timezone.utc) - self.last_heartbeat.replace(tzinfo=timezone.utc))
        except (TypeError, AttributeError):
            return CodeHost.to_minutes(datetime.now(timezone.utc) - self.created_at.replace(tzinfo=timezone.utc))

    @hybrid_property
    def modified_ago(self) -> int:
        """Time since last file modification in minutes"""

        try:
            return CodeHost.to_minutes(datetime.now(timezone.utc) - self.last_utilization.replace(tzinfo=timezone.utc))
        except (TypeError, AttributeError):
            return CodeHost.to_minutes(datetime.now(timezone.utc) - self.created_at.replace(tzinfo=timezone.utc))

    @hybrid_property
    def is_quiescent(self) -> bool:
        """Is the host still being used? True if the last heartbeat is more than 20 minutes ago, 
        or the last file modification is more than 15 minutes ago."""

        return self.heart_beat_ago > 20 or self.modified_ago > 15

    @hybrid_property
    def is_mia(self) -> bool:
        """Is the host missing in action? True if the host record doesn't have
        a container id, or similar """
        
        return self.app_state == HostState.MIA.value or self.state == HostState.MIA.value

    def update_from_ci(self, ci):
        self.service_name = ci["service_name"]
        self.container_id = ci["container_id"]
        self.node_id = ci["node_id"]
        self.state = ci["state"]
        self.public_url = ci["hostname"]

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
            "proto_id": self.proto_id,
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
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data):
        data["last_stats"] = datetime.fromisoformat(data["last_stats"]) if data.get("last_stats") else None
        data["last_heartbeat"] = datetime.fromisoformat(data["last_heartbeat"]) if data.get("last_heartbeat") else None
        data["last_utilization"] = (
            datetime.fromisoformat(data["last_utilization"]) if data.get("last_utilization") else None
        )

        data["created_at"] = (
            datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(timezone.utc)
        )
        data["updated_at"] = (
            datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else datetime.now(timezone.utc)
        )
        return cls(**data)

    def update_telemetry(self, telemetry: TelemetryReport):
        try:
            # This finds the time of the last file modification
            # but there may be more recent keystrokes.
            max_last_modified = max(file_stat.lastModified for file_name, file_stat in telemetry.fileStats.items())
        except ValueError:
            max_last_modified = None

        self.last_stats = telemetry.timestamp

        self.last_heartbeat = telemetry.timestamp  # TIme of
        self.last_utilization = max_last_modified

        self.memory_usage = telemetry.sysMemory
        self.user_activity_rate = telemetry.average5m

        self.utilization_1 = telemetry.average30m
        self.utilization_2 = telemetry.average1m

    def update_stats(self, record: dict):
        record["last_heartbeat"] = datetime.now().astimezone().isoformat()  # set this for every record

        if record.get("utilization_1") is not None or record.get("utilization_2") is not None:
            record["last_utilization"] = datetime.now().astimezone().isoformat()

        if record["memory_usage"] is not None:
            record["last_stats"] = datetime.now().astimezone().isoformat()

        if record["username"] is None:
            record["username"] = record["labels"].get("jtl.codeserver.username")

    def __repr__(self):
        return f"<CodeHost(id={self.id}, user_id={self.user_id}, service_id={self.service_id})>"


class ClassProto(db.Model):
    """A template for a class. It describes the proto and repo to use for a class."""

    __tablename__ = "class_proto"

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
    creator = relationship("User", backref="class_proto")

    created_at = Column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime, default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc), nullable=False
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

    classes = relationship("Class", back_populates="proto")

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
        data["created_at"] = (
            datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(timezone.utc)
        )
        data["updated_at"] = (
            datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else datetime.now(timezone.utc)
        )
        return cls(**data)


event.listen(ClassProto, "before_insert", ClassProto.set_hash)
event.listen(ClassProto, "before_update", ClassProto.set_hash)


def ensure_database_exists(app: Flask):
    uri = app.db.engine.url
    engine = create_engine(uri)
    if not database_exists(engine.url):
        create_database(engine.url)


def export_dict():
    users = [u.to_dict() for u in User.query.all()]
    classes = [c.to_dict() for c in Class.query.all()]
    protos = [i.to_dict() for i in ClassProto.query.all()]
    hosts = [h.to_dict() for h in CodeHost.query.all()]

    return {"users": users, "proto": protos, "classes": classes, "hosts": hosts}


def import_dict(data):
    db.create_all()

    for user_data in data["users"]:
        user = User.from_dict(user_data)
        db.session.add(user)

    db.session.commit()

    for proto in data["protos"]:
        proto = ClassProto.from_dict(proto)
        db.session.add(proto)

    db.session.commit()

    for class_data in data["classes"]:
        class_ = Class.from_dict(class_data)
        db.session.add(class_)

    db.session.commit()

    for host_data in data.get("hosts", []):
        host = CodeHost.from_dict(host_data)
        db.session.add(host)

    db.session.commit()
