import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Table, Text, func, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy_utils import PasswordType
from flask_login import LoginManager, UserMixin
from flask_dance.consumer.storage.sqla import OAuthConsumerMixin
from cspawn.init import db
from cspawn.auth.models import User

class Class(db.Model):
    __tablename__ = "classes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    location = Column(String(255), nullable=True)
    reference = Column(String(255), nullable=True)  # URL or other reference
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    recurrence_rule = Column(String(255), nullable=True) 
    container_image = Column(String(255), nullable=False)
    start_script = Column(Text, nullable=False)

    instructors = relationship(
        "User", secondary="class_instructors", back_populates="classes"
    )
    students = relationship(
        "User", secondary="class_students", back_populates="classes"
    )

    def __repr__(self):
        return f"<Class(id={self.id}, name={self.name})>"

