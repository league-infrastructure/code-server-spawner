import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Table, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy_utils import PasswordType
from flask_login import LoginManager, UserMixin
from flask_dance.consumer.storage.sqla import OAuthConsumerMixin
from cspawn.init import db


# Association table for many-to-many relationship between User and Role
user_roles = Table(
    "user_roles",
    db.metadata,
    Column("user_id", UUID(as_uuid=True),
           ForeignKey("users.id"), primary_key=True),
    Column("role_id", UUID(as_uuid=True),
           ForeignKey("roles.id"), primary_key=True),
)

# Association table for many-to-many relationship between Class and Student
class_students = Table(
    "class_students",
    db.metadata,
    Column("class_id", UUID(as_uuid=True),
           ForeignKey("classes.id"), primary_key=True),
    Column(
        "student_id", UUID(as_uuid=True), ForeignKey("students.id"), primary_key=True
    ),
)

# Association table for many-to-many relationship between Class and Instructor
class_instructors = Table(
    "class_instructors",
    db.metadata,
    Column("class_id", UUID(as_uuid=True),
           ForeignKey("classes.id"), primary_key=True),
    Column(
        "instructor_id",
        UUID(as_uuid=True),
        ForeignKey("instructors.id"),
        primary_key=True,
    ),
)


class Class(db.Model):
    __tablename__ = "classes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    location = Column(String(255), nullable=True)
    reference = Column(String(255), nullable=True)  # URL or other reference
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    recurrence_rule = Column(
        String(255), nullable=True
    )  # Recurrence rule (e.g., iCalendar format)
    container_image = Column(String(255), nullable=False)
    start_script = Column(Text, nullable=False)

    instructors = relationship(
        "Instructor", secondary="class_instructors", back_populates="classes"
    )
    students = relationship(
        "Student", secondary="class_students", back_populates="classes"
    )

    def __repr__(self):
        return f"<Class(id={self.id}, name={self.name})>"


class Role(db.Model):
    __tablename__ = "roles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(50), unique=True, nullable=False)
    description = Column(String(255), nullable=True)

    users = relationship("User", secondary=user_roles, back_populates="roles")

    def __repr__(self):
        return f"<Role(id={self.id}, name={self.name})>"


class Student(Role):
    __tablename__ = "students"
    id = Column(UUID(as_uuid=True), ForeignKey("roles.id"), primary_key=True)
    classes = relationship(
        "Class", secondary="class_students", back_populates="students"
    )


class Instructor(Role):
    __tablename__ = "instructors"
    id = Column(UUID(as_uuid=True), ForeignKey("roles.id"), primary_key=True)
    classes = relationship(
        "Class", secondary="class_instructors", back_populates="instructors"
    )


class Administrator(Role):
    __tablename__ = "administrators"
    id = Column(UUID(as_uuid=True), ForeignKey("roles.id"), primary_key=True)
