from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
import database
import processor
import uuid
import os
import subprocess
import json

app = FastAPI()

# Add CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For production, replace "*" with the actual extension ID or origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Database
database.init_db()

# Dependency to get DB session
def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_user_by_key(db: Session, api_key: str):
    return db.query(database.User).filter(database.User.api_key == api_key).first()

@app.get("/v1/youtube/check")
async def check_video(
    videoId: str, 
    lang: str, 
    x_api_key: str = Header(None), 
    db: Session = Depends(get_db)
):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing API Key")
    
    user = get_user_by_key(db, x_api_key)
    if not user:
        raise HTTPException(status_code=403, detail="Invalid API Key")

    # 1. Check if user already unlocked this video
    video = db.query(database.Video).filter(database.Video.youtube_id == videoId).first()
    if video:
        unlocked = db.query(database.UserUnlockedVideo).filter(
            database.UserUnlockedVideo.user_id == user.id,
            database.UserUnlockedVideo.video_id == video.id,
            database.UserUnlockedVideo.language == lang
        ).first()
        
        if unlocked:
            transcription = db.query(database.Transcription).filter(
                database.Transcription.video_id == video.id,
                database.Transcription.language == lang
            ).first()
            if transcription:
                return {"status": "unlocked", "segments": transcription.segments_json}

        # 2. If video exists but not unlocked, check if transcription exists globally
        transcription = db.query(database.Transcription).filter(
            database.Transcription.video_id == video.id,
            database.Transcription.language == lang
        ).first()
        
        if transcription:
            # Check balance
            if user.available_minutes < video.duration_minutes:
                return {"status": "insufficient_balance", "required": video.duration_minutes, "available": user.available_minutes}
            
            # Deduct and Unlock
            user.available_minutes -= video.duration_minutes
            new_unlock = database.UserUnlockedVideo(user_id=user.id, video_id=video.id, language=lang)
            db.add(new_unlock)
            db.commit()
            return {"status": "unlocked", "segments": transcription.segments_json}

    return {"status": "requires_audio"}

@app.post("/v1/youtube/upload-audio")
async def upload_audio(
    background_tasks: BackgroundTasks,
    videoId: str = Form(...),
    lang: str = Form(...),
    duration: float = Form(...),
    audioFile: UploadFile = File(...),
    x_api_key: str = Header(None),
    db: Session = Depends(get_db)
):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing API Key")
    
    user = get_user_by_key(db, x_api_key)
    if not user:
        raise HTTPException(status_code=403, detail="Invalid API Key")

    # Convert duration from seconds to minutes for balance check
    duration_minutes = duration / 60.0

    # Check balance
    if user.available_minutes < duration_minutes:
        raise HTTPException(status_code=402, detail=f"Insufficient balance. Need {duration_minutes:.1f} mins, have {user.available_minutes:.1f}")

    # Save video metadata or update duration
    video = db.query(database.Video).filter(database.Video.youtube_id == videoId).first()
    if not video:
        video = database.Video(youtube_id=videoId, duration_minutes=duration_minutes)
        db.add(video)
        db.commit()
        db.refresh(video)
    
    # Save audio file temporarily
    temp_filename = f"temp_{videoId}_{uuid.uuid4()}.webm"
    temp_path = os.path.join("temp_audio", temp_filename)
    os.makedirs("temp_audio", exist_ok=True)
    
    with open(temp_path, "wb") as f:
        f.write(await audioFile.read())

    # Trigger Translation Processing in background
    print(f"[Upload] Received audio for {videoId} | Target: {lang}")
    background_tasks.add_task(
        processor.process_audio, 
        temp_path, 
        videoId, 
        lang, 
        database.SessionLocal
    )

    return {"status": "processing", "message": f"Audio received. Translation to {lang} started."}

# Stream URL resolver — uses yt-dlp Python API (single call, no subprocess overhead)
# Returns the CDN URL so the CLIENT can download audio using their own IP
@app.get("/v1/youtube/get-stream-url")
async def get_stream_url(
    videoId: str,
    x_api_key: str = Header(None),
    db: Session = Depends(get_db)
):
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing API Key")
    user = get_user_by_key(db, x_api_key)
    if not user:
        raise HTTPException(status_code=403, detail="Invalid API Key")

    import time
    t0 = time.time()
    print(f"[StreamURL] Resolving stream URL for {videoId}...")

    url = f"https://www.youtube.com/watch?v={videoId}"
    try:
        import yt_dlp
        ydl_opts = {
            # Whisper only needs speech clarity — lowest bitrate is fine
            # Smaller file = faster CDN download from client's browser
            'format': 'worstaudio[ext=webm]/worstaudio[ext=m4a]/worstaudio/bestaudio',
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        stream_url = info.get('url')
        duration = info.get('duration', 0)
        filesize = info.get('filesize') or info.get('filesize_approx') or 0
        video_language = info.get('language') or 'unknown'
        category = info.get('categories', ['unknown'])[0]

        if not stream_url:
            raise HTTPException(status_code=500, detail="yt-dlp returned no stream URL")

        print(f"[StreamURL] ✅ {videoId} | Lang: {video_language} | Cat: {category} | {filesize} bytes")
        return {
            "stream_url": stream_url, 
            "duration": duration, 
            "filesize": filesize,
            "video_language": video_language,
            "category": category
        }

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "is not available" in msg or "Private video" in msg:
            raise HTTPException(status_code=404, detail="This YouTube video is private or unavailable.")
        raise HTTPException(status_code=500, detail=f"yt-dlp error: {msg[:200]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Helper to add a test user
@app.get("/debug/create-user")
def create_test_user(api_key: str, minutes: float, db: Session = Depends(get_db)):
    user = database.User(api_key=api_key, available_minutes=minutes)
    db.add(user)
    db.commit()
    return {"status": "ok", "user_id": user.id}
