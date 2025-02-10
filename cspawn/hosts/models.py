from sqlalchemy import Column, String, Integer, Float, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from cspawn.init import db
from cspawn.users.models import Role


class CodeHost(db.Model):
    __tablename__ = 'code_host'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    role_id = Column(Integer, ForeignKey('roles.id'), nullable=False)
    role = relationship("Role", backref="code_host")
    
        
    service_id = Column(String, nullable=False)
    service_name = Column(String, nullable=False)
    container_id = Column(String, nullable=False)
    container_name = Column(String, nullable=False)
    
    created_at = Column(DateTime, default=datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=datetime.now(timezone.utc), onupdate=datetime.now(timezone.utc), nullable=False)
    
    keystroke_reports = relationship("KeystrokeReport", backref="code_host")


# Reports from the keystroke monitor. There are also Pydantic models in the keyrate.py file

class FileStat(db.Model):
    __tablename__ = 'file_stats'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    keystrokes = Column(Integer, nullable=False)
    last_modified = Column(String, nullable=False)

class KeystrokeReport(db.Model):
    __tablename__ = 'keystroke_reports'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    code_host_id = Column(Integer, ForeignKey('code_host.id'), nullable=False)
    code_host = relationship("CodeHost", backref="keystroke_reports")
    
    timestamp = Column(String, nullable=False)
    instance_id = Column(String, nullable=False)
    
    keystrokes = Column(Integer, nullable=False)
    average30m = Column(Float, nullable=False)
    reporting_rate = Column(Integer, nullable=False)

    file_stats = relationship("FileStat", backref="keystroke_report")

    @staticmethod
    def add_ks_report(code_host_id, data):
        # Create the KeystrokeReport object
        keystroke_report = KeystrokeReport(
            code_host_id=code_host_id,
            timestamp=data['timestamp'],
            instance_id=data['instance_id'],
            keystrokes=data['keystrokes'],
            average30m=data['average30m'],
            reporting_rate=data['reporting_rate']
        )
        
        # Create the related FileStat objects
        for file_stat_data in data['file_stats']:
            file_stat = FileStat(
                keystrokes=file_stat_data['keystrokes'],
                last_modified=file_stat_data['last_modified']
            )
            keystroke_report.file_stats.append(file_stat)
    
        # Add the KeystrokeReport object to the session
        db.session.add(keystroke_report)
        db.session.commit()

