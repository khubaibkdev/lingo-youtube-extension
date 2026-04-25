from sqlalchemy import Column, String, Float, ForeignKey, DateTime, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
import datetime
import uuid

import os
from dotenv import load_dotenv

load_dotenv()

# Use an environment variable for the Postgres URL
# Format: postgresql://user:password@host:port/dbname
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/subflow")

engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    api_key = Column(String, unique=True, index=True)
    available_minutes = Column(Float, default=0.0)

class Video(Base):
    __tablename__ = "videos"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    youtube_id = Column(String, unique=True, index=True)
    duration_minutes = Column(Float)

class Transcription(Base):
    __tablename__ = "transcriptions"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    video_id = Column(String, ForeignKey("videos.id"))
    language = Column(String)  # 'ps' or 'ba'
    segments_json = Column(JSONB)  # Store the actual array (Postgres JSONB)
    
    video = relationship("Video")

class UserUnlockedVideo(Base):
    __tablename__ = "user_unlocked_videos"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"))
    video_id = Column(String, ForeignKey("videos.id"))
    language = Column(String)
    unlocked_at = Column(DateTime, default=datetime.datetime.utcnow)

def init_db():
    Base.metadata.create_all(bind=engine)
