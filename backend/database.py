from sqlalchemy import Column, String, Float, ForeignKey, DateTime, UniqueConstraint, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import datetime
import uuid

import os
from dotenv import load_dotenv

load_dotenv()

# Format: postgresql://user:password@host:port/dbname
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/subflow")

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


class Video(Base):
    __tablename__ = "videos"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    youtube_id = Column(String, unique=True, index=True)
    duration_minutes = Column(Float)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class Transcription(Base):
    __tablename__ = "transcriptions"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    video_id = Column(String, ForeignKey("videos.id"))
    language = Column(String)  # 'ps', 'bal', etc.
    segments_json = Column(JSONB)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    video = relationship("Video")

    __table_args__ = (
        UniqueConstraint("video_id", "language", name="uq_transcription_video_lang"),
    )


class ApiKeyUnlock(Base):
    """Records that a given (hashed) API key has paid to access a (video, language).
    Replaces v1's `user_unlocked_videos` table. We store hash(pepper + api_key),
    never the raw key."""
    __tablename__ = "api_key_unlocks"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    api_key_hash = Column(String, index=True)
    video_id = Column(String, ForeignKey("videos.id"))
    language = Column(String)
    unlocked_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("api_key_hash", "video_id", "language", name="uq_unlock_key_video_lang"),
    )


def init_db():
    Base.metadata.create_all(bind=engine)
