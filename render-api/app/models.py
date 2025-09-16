"""SQLAlchemy models for the render API."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db import Base


class Project(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=dt.datetime.utcnow,
        onupdate=dt.datetime.utcnow,
        nullable=False,
    )
    last_output_name = Column(String, nullable=True)

    jobs = relationship("Job", back_populates="project", cascade="all, delete-orphan")
    artifacts = relationship("Artifact", back_populates="project", cascade="all, delete-orphan")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False)
    status = Column(String, nullable=False)  # QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED
    payload = Column(JSON, nullable=False)
    progress = Column(Float, default=0.0, nullable=False)
    stage = Column(String, default="", nullable=False)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime,
        default=dt.datetime.utcnow,
        onupdate=dt.datetime.utcnow,
        nullable=False,
    )

    project = relationship("Project", back_populates="jobs")
    artifacts = relationship("Artifact", back_populates="job", cascade="all, delete-orphan")


class Artifact(Base):
    __tablename__ = "artifacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False)
    job_id = Column(String, ForeignKey("jobs.id"), nullable=True)
    path = Column(String, nullable=False)
    kind = Column(String, nullable=False)
    size = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)

    project = relationship("Project", back_populates="artifacts")
    job = relationship("Job", back_populates="artifacts")
