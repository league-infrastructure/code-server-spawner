import uuid

from flask_login import UserMixin
from sqlalchemy import Boolean, Column, DateTime, String, func, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy_utils import PasswordType

from cspawn.init import db

class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=True)
    email = Column(String(255), unique=True, nullable=True, index=True)
    password = Column(PasswordType(schemes=['bcrypt']), nullable=True)
    
    # OAuth fields
    oauth_provider = Column(String(50), nullable=True)  # 'google', 'github', 'cleaver'
    oauth_id = Column(String(255), unique=True, nullable=True)  # Provider-specific ID
    avatar_url = Column(String(500), nullable=True)
    
    is_admin = Column(Boolean, default=False, nullable=False)
    is_student = Column(Boolean, default=False, nullable=False)
    is_instructor = Column(Boolean, default=False, nullable=False)
    display_name = Column(String(255), nullable=True)
    birth_year = Column(Integer, nullable=True)
    
    created_at = Column(DateTime, default=func.now())

    def __repr__(self):
        return f"<User(id={self.id}, username={self.username}, email={self.email}, provider={self.oauth_provider})>"

