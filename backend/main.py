from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

import os
import uuid

import database
import platform_client
import processor

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

database.init_db()


def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_api_key(x_api_key: str | None) -> str:
    """Header presence check only. Real validation happens via platform."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing API Key")
    return x_api_key


def assert_key_valid(api_key: str) -> platform_client.DeductResult:
    """Validate the key with the platform (60s cached). 401 on invalid."""
    r = platform_client.validate(api_key)
    if not r.valid:
        raise HTTPException(status_code=401, detail=r.error or "Invalid API Key")
    return r


@app.get("/v1/youtube/check")
async def check_video(
    videoId: str,
    lang: str,
    x_api_key: str = Header(None),
    db: Session = Depends(get_db),
):
    api_key = require_api_key(x_api_key)
    api_key_hash = platform_client.hash_api_key(api_key)

    video = db.query(database.Video).filter(database.Video.youtube_id == videoId).first()

    if video:
        # Path 1: this key already unlocked the (video, lang) → serve cache, validate only.
        unlock = db.query(database.ApiKeyUnlock).filter(
            database.ApiKeyUnlock.api_key_hash == api_key_hash,
            database.ApiKeyUnlock.video_id == video.id,
            database.ApiKeyUnlock.language == lang,
        ).first()

        transcription = db.query(database.Transcription).filter(
            database.Transcription.video_id == video.id,
            database.Transcription.language == lang,
        ).first()

        if unlock and transcription:
            # Re-view: validate key only (cached); never charge.
            assert_key_valid(api_key)
            return {"status": "unlocked",
                    "segments": processor.clean_segments(transcription.segments_json, lang)}

        # Path 2: cache exists but this key hasn't paid → charge via /v1/deduct.
        if transcription:
            r = platform_client.deduct(api_key, video.duration_minutes)
            if not r.valid:
                raise HTTPException(status_code=401, detail=r.error or "Invalid API Key")
            if r.quota_exceeded:
                return {
                    "status": "quota_exceeded",
                    "balance_minutes": r.balance_minutes,
                    "required": video.duration_minutes,
                }
            # Success → record unlock, serve cache.
            db.add(database.ApiKeyUnlock(
                api_key_hash=api_key_hash, video_id=video.id, language=lang,
            ))
            try:
                db.commit()
            except Exception:
                # Concurrent insert — ON CONFLICT not native to SA core w/o dialect; absorb.
                db.rollback()
            return {"status": "unlocked",
                    "segments": processor.clean_segments(transcription.segments_json, lang)}

    # Path 3: no cache yet — must process audio. Validate key so abuse of the
    # follow-up /get-stream-url + /upload-audio chain is gated.
    assert_key_valid(api_key)
    return {"status": "requires_audio"}


@app.get("/v1/youtube/get-stream-url")
async def get_stream_url(
    videoId: str,
    x_api_key: str = Header(None),
    db: Session = Depends(get_db),
):
    api_key = require_api_key(x_api_key)
    assert_key_valid(api_key)

    import time
    t0 = time.time()
    print(f"[StreamURL] Resolving stream URL for {videoId}...")

    url = f"https://www.youtube.com/watch?v={videoId}"
    try:
        import yt_dlp
        ydl_opts = {
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
        category = (info.get('categories') or ['unknown'])[0]

        if not stream_url:
            raise HTTPException(status_code=500, detail="yt-dlp returned no stream URL")

        print(f"[StreamURL] ✅ {videoId} | Lang: {video_language} | Cat: {category} | "
              f"{filesize} bytes | {time.time()-t0:.1f}s")
        return {
            "stream_url": stream_url,
            "duration": duration,
            "filesize": filesize,
            "video_language": video_language,
            "category": category,
        }

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "is not available" in msg or "Private video" in msg:
            raise HTTPException(status_code=404, detail="This YouTube video is private or unavailable.")
        raise HTTPException(status_code=500, detail=f"yt-dlp error: {msg[:200]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/v1/youtube/upload-audio")
async def upload_audio(
    videoId: str = Form(...),
    lang: str = Form(...),
    duration: float = Form(...),
    audioFile: UploadFile = File(...),
    x_api_key: str = Header(None),
    db: Session = Depends(get_db),
):
    api_key = require_api_key(x_api_key)
    duration_minutes = duration / 60.0

    # Pre-flight: validate key + check balance (no deduction yet).
    pre = assert_key_valid(api_key)
    if pre.balance_minutes is not None and pre.balance_minutes < duration_minutes:
        return {
            "status": "quota_exceeded",
            "balance_minutes": pre.balance_minutes,
            "required": duration_minutes,
        }

    # Persist video metadata (idempotent).
    video = db.query(database.Video).filter(database.Video.youtube_id == videoId).first()
    if not video:
        video = database.Video(youtube_id=videoId, duration_minutes=duration_minutes)
        db.add(video)
        db.commit()
        db.refresh(video)

    # Save audio to temp file.
    temp_filename = f"temp_{videoId}_{uuid.uuid4()}.webm"
    temp_path = os.path.join("temp_audio", temp_filename)
    os.makedirs("temp_audio", exist_ok=True)
    with open(temp_path, "wb") as f:
        f.write(await audioFile.read())

    print(f"[Upload] Received audio for {videoId} | Target: {lang} | {duration_minutes:.2f}min")

    # Process synchronously via the AI server.
    segments = processor.process_audio(temp_path, videoId, lang, database.SessionLocal)
    if not segments:
        raise HTTPException(
            status_code=502,
            detail=f"Translation engine returned no segments for lang='{lang}'. "
                   f"Check the [Processor] log for the AI server's response body.",
        )

    # Charge after successful processing.
    charge = platform_client.deduct(api_key, duration_minutes)
    if not charge.valid:
        # Key revoked between pre-check and charge. Edge case — work was done, no charge.
        # We DON'T record an unlock (no payment) and surface the auth error.
        raise HTTPException(status_code=401, detail=charge.error or "Invalid API Key")
    if charge.quota_exceeded:
        # Race: balance dropped between pre-check and charge. Same handling.
        return {
            "status": "quota_exceeded",
            "balance_minutes": charge.balance_minutes,
            "required": duration_minutes,
        }

    # Charge succeeded → record unlock so re-views are free.
    api_key_hash = platform_client.hash_api_key(api_key)
    existing = db.query(database.ApiKeyUnlock).filter(
        database.ApiKeyUnlock.api_key_hash == api_key_hash,
        database.ApiKeyUnlock.video_id == video.id,
        database.ApiKeyUnlock.language == lang,
    ).first()
    if not existing:
        db.add(database.ApiKeyUnlock(
            api_key_hash=api_key_hash, video_id=video.id, language=lang,
        ))
        try:
            db.commit()
        except Exception:
            db.rollback()

    return {"status": "unlocked", "segments": segments}
