from cspawn.main.models import db


from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)

from typing import Union

from sqlalchemy.orm import relationship


from datetime import datetime, timezone
from hashlib import md5
from sqlalchemy import event


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


event.listen(HostImage, "before_insert", HostImage.set_hash)
event.listen(HostImage, "before_update", HostImage.set_hash)
