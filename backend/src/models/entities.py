from sqlalchemy import String, Float, Integer, ForeignKey, JSON, Text, DateTime, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, timezone
from src.config.database import Base
def utcnow(): return datetime.now(timezone.utc)
class User(Base):
    __tablename__="users"
    id:Mapped[int]=mapped_column(primary_key=True)
    email:Mapped[str]=mapped_column(String(100),unique=True)
    password_hash:Mapped[str]=mapped_column(String(300))
    role:Mapped[str]=mapped_column(String(20))
class Batch(Base):
    __tablename__="batches"
    id:Mapped[int]=mapped_column(primary_key=True)
    code:Mapped[str]=mapped_column(String(50),unique=True)
    line:Mapped[str]=mapped_column(String(20))
    supplier:Mapped[str]=mapped_column(String(80))
    tonnes:Mapped[float]=mapped_column(Float)
    received_date:Mapped[str]=mapped_column(String(10))
class Inspection(Base):
    __tablename__="inspections"
    id:Mapped[int]=mapped_column(primary_key=True)
    sample_code:Mapped[str]=mapped_column(String(70),unique=True)
    batch_id:Mapped[int]=mapped_column(ForeignKey("batches.id"))
    flour_yield:Mapped[float]=mapped_column(Float)
    moisture:Mapped[float]=mapped_column(Float)
    inspector:Mapped[str]=mapped_column(String(50))
class Review(Base):
    __tablename__="reviews"
    id:Mapped[int]=mapped_column(primary_key=True)
    title:Mapped[str]=mapped_column(String(120))
    line:Mapped[str]=mapped_column(String(20))
    status:Mapped[str]=mapped_column(String(20),default="queued",server_default="queued")
    result:Mapped[dict]=mapped_column(JSON,default=dict)
    snapshot:Mapped[list]=mapped_column(JSON,default=list)
    note:Mapped[str]=mapped_column(Text,default="",server_default="")
    created_by:Mapped[str]=mapped_column(String(100))
    approved_by:Mapped[str]=mapped_column(String(100),default="",server_default="")
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    # Job lifecycle. The database, not Redis, is the authority for who may publish a result.
    attempt_count:Mapped[int]=mapped_column(Integer,default=0,server_default=text("0"),nullable=False)
    max_attempts:Mapped[int]=mapped_column(Integer,default=3,server_default=text("3"),nullable=False)
    execution_token:Mapped[str]=mapped_column(String(64),default="",server_default="",nullable=False)
    lease_owner:Mapped[str]=mapped_column(String(128),default="",server_default="",nullable=False)
    lease_expires_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    next_attempt_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    completed_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    approved_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    failure_reason:Mapped[str]=mapped_column(Text,default="",server_default="",nullable=False)
    snapshot_checksum:Mapped[str]=mapped_column(String(64),default="",server_default="",nullable=False)
    artifact_checksum:Mapped[str]=mapped_column(String(64),default="",server_default="",nullable=False)
class ReviewAttempt(Base):
    __tablename__="review_attempts"
    __table_args__=(UniqueConstraint("review_id","attempt_no",name="uq_review_attempt_no"),)
    id:Mapped[int]=mapped_column(primary_key=True)
    review_id:Mapped[int]=mapped_column(ForeignKey("reviews.id"),index=True)
    attempt_no:Mapped[int]=mapped_column(Integer)
    worker_id:Mapped[str]=mapped_column(String(128),default="",server_default="")
    execution_token:Mapped[str]=mapped_column(String(64),default="",server_default="",index=True)
    status:Mapped[str]=mapped_column(String(20),default="running",server_default="running")
    phase:Mapped[str]=mapped_column(String(20),default="claimed",server_default="claimed")
    error:Mapped[str]=mapped_column(Text,default="",server_default="")
    started_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    finished_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    heartbeat_at:Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
class Audit(Base):
    __tablename__="audits"
    id:Mapped[int]=mapped_column(primary_key=True)
    actor:Mapped[str]=mapped_column(String(100))
    action:Mapped[str]=mapped_column(String(200))
    status:Mapped[int]=mapped_column(Integer)
    created_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
