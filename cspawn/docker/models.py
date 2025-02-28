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
from sqlalchemy.orm import relationship


from datetime import datetime, timezone


class CodeHost(db.Model):
    __tablename__ = "code_host"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user = relationship("User", backref="code_hosts")

    service_id = Column(String, nullable=False)
    service_name = Column(String, nullable=False)
    container_id = Column(String, nullable=True)
    container_name = Column(String, nullable=True)

    state = Column(String, default="unknown", nullable=False)

    host_image_id = Column(Integer, ForeignKey("host_images.id"), nullable=True)
    host_image = relationship("HostImage", backref="code_hosts")

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

    def update_stats(self, record: Union[dict, DockerContainerStats]):

        if not isinstance(record, dict):
            record = record.model_dump(exclude_none=False)
        else:
            record = DockerContainerStats(**record).model_dump(exclude_none=False)

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

        try:
            if "container_id" not in record:
                raise ValueError("Record must have an 'container_id' value")

            self.collection.insert_one(record)
        except (DuplicateKeyError, ValueError) as e:

            record.pop("_id", None)

            # Never update field values to None
            update_fields = {k: v for k, v in record.items() if v is not None}

            self.collection.update_one(
                {"container_id": record["container_id"]}, {"$set": update_fields}
            )

    def __repr__(self):
        return f"<CodeHost(id={self.id}, user_id={self.user_id}, service_id={self.service_id})>"


class HostImage(db.Model):
    __tablename__ = "host_images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
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

    classes = relationship(
        "Class", secondary="class_host_images", back_populates="host_images"
    )
