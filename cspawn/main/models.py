"""
Database Models
"""

import json

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    func,
)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy_utils import PasswordType


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

    # OAuth fields
    oauth_provider = Column(String(50), nullable=True)  # 'google', 'github', 'cleaver'
    oauth_id = Column(String(255), unique=True, nullable=True)  # Provider-specific ID
    avatar_url = Column(String(500), nullable=True)

    is_active = Column(Boolean, default=True, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    is_student = Column(Boolean, default=False, nullable=False)
    is_instructor = Column(Boolean, default=False, nullable=False)

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

    display_name = Column(String(255), nullable=True)
    birth_year = Column(Integer, nullable=True)

    created_at = Column(DateTime, default=func.now)

    # Add the relationships for classes_instructing and classes_taking
    classes_instructing = relationship(
        "Class", secondary="class_instructors", back_populates="instructors"
    )
    classes_taking = relationship(
        "Class", secondary="class_students", back_populates="students"
    )

    def __repr__(self):
        return (
            f"<User(id={self.id}, username={self.username}, "
            f"email={self.email}, provider={self.oauth_provider})>"
        )


class Class(db.Model):
    """A collections of students and instructors"""

    __tablename__ = "classes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    location = Column(String(255), nullable=True)
    reference = Column(String(255), nullable=True)  # URL or other reference
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=True)
    recurrence_rule = Column(String(255), nullable=True)

    start_script = Column(Text, nullable=False)

    class_code = Column(String(20), nullable=True)

    instructors = relationship(
        "User", secondary="class_instructors", back_populates="classes_instructing"
    )
    students = relationship(
        "User", secondary="class_students", back_populates="classes_taking"
    )

    host_images = relationship(
        "HostImage", secondary="class_host_images", back_populates="classes"
    )

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

class_host_images = Table(
    "class_host_images",
    db.Model.metadata,
    Column("class_id", Integer, ForeignKey("classes.id"), primary_key=True),
    Column("host_image_id", Integer, ForeignKey("host_images.id"), primary_key=True),
)
