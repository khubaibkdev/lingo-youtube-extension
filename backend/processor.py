import os
import json
import subprocess
import time
import requests
from sqlalchemy.orm import Session
import database

AI_SERVER_URL = "http://192.168.18.119:8004/translate-audio"


LANG_MAP = {
    "ps": "ps",
    "bal": "bal"
}

# Resolve a working ffmpeg binary. Searches several locations so it works
# whether uvicorn runs inside the venv or from system Python.
def _resolve_ffmpeg() -> str:
    # 1. imageio-ffmpeg in the *currently running* Python
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if os.path.exists(path):
            print(f"[Processor] ffmpeg: bundled (current python) {path}")
            return path
    except ImportError:
        pass

    # 2. The venv directory next to this file — handles the case where uvicorn
    #    is launched from system Python but the project's venv has imageio-ffmpeg.
    here = os.path.dirname(os.path.abspath(__file__))
    venv_bins = os.path.join(here, ".venv", "Lib", "site-packages", "imageio_ffmpeg", "binaries")
    if os.path.isdir(venv_bins):
        for fname in os.listdir(venv_bins):
            if fname.lower().startswith("ffmpeg") and fname.lower().endswith(".exe"):
                path = os.path.join(venv_bins, fname)
                print(f"[Processor] ffmpeg: venv-side binary {path}")
                return path

    # 3. ffmpeg on PATH (system install)
    from shutil import which
    path = which("ffmpeg")
    if path:
        print(f"[Processor] ffmpeg: system PATH {path}")
        return path

    print(
        "[Processor] ⚠️  ffmpeg NOT FOUND in: imageio-ffmpeg, "
        f"{venv_bins}, or system PATH. "
        "Install: python -m pip install imageio-ffmpeg"
    )
    return "ffmpeg"  # placeholder; will fail at call time with a clear error


FFMPEG_BIN = _resolve_ffmpeg()


def probe_duration(media_path: str) -> float | None:
    """Return the duration in seconds via ffprobe (which ships next to ffmpeg).
    Returns None if probing fails."""
    ffprobe = FFMPEG_BIN.replace("ffmpeg", "ffprobe") if "ffmpeg" in FFMPEG_BIN else "ffprobe"
    if not os.path.exists(ffprobe):
        ffprobe = "ffprobe"
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", media_path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def convert_webm_to_wav(webm_path: str) -> str | None:
    """Convert .webm/opus → .wav (16 kHz mono 16-bit PCM, the Whisper-friendly
    shape). Returns the wav path on success, None on failure. The wav file
    is the caller's responsibility to clean up."""
    wav_path = os.path.splitext(webm_path)[0] + ".wav"
    print(f"[Processor] 🔄 Converting webm → wav (16kHz mono PCM)")
    t = time.time()
    try:
        result = subprocess.run(
            [
                FFMPEG_BIN,
                "-y",                  # overwrite output
                "-loglevel", "error",  # silent unless something breaks
                "-i", webm_path,
                "-ar", "16000",        # 16 kHz sample rate
                "-ac", "1",            # mono
                "-acodec", "pcm_s16le",
                wav_path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"[Processor] ❌ ffmpeg invocation failed: {e}")
        return None
    if result.returncode != 0:
        print(f"[Processor] ❌ ffmpeg returned {result.returncode}")
        if result.stderr:
            print(f"[Processor]    └─ stderr: {result.stderr.strip()[-500:]}")
        return None
    print(f"[Processor]    └─ wav: {os.path.getsize(wav_path):,} bytes in {time.time()-t:.1f}s")
    return wav_path


def clean_segments(segments: list, lang: str) -> list:
    """Remove '__<lang>__' markers from translation text. Handles prefix, suffix,
    or anywhere-in-the-middle. Mutates in place AND returns the list."""
    marker = f"__{lang}__"
    for seg in segments or []:
        t = seg.get('translation')
        if isinstance(t, str) and marker in t:
            seg['translation'] = t.replace(marker, '').strip()
    return segments

def process_audio(file_path: str, video_id: str, lang: str, db_session_factory):
    """
    Sends audio to the external AI server for transcription/translation and saves result to DB.
    """
    overall_start = time.time()
    wav_path: str | None = None
    print(f"\n[Processor] ========== Forwarding to AI Server: {video_id} ({lang}) ==========")

    try:
        # Step 1: Prepare files and data for external API
        if not os.path.exists(file_path):
            print(f"[Processor] ❌ File not found: {file_path}")
            return None

        # AI server expects WAV (PCM 16kHz mono). Convert from browser's webm.
        wav_path = convert_webm_to_wav(file_path)
        if not wav_path:
            return None

        ai_lang = LANG_MAP.get(lang, lang)  # forward extension's lang to AI server
        upload_size = os.path.getsize(wav_path)
        with open(wav_path, 'rb') as f:
            files = {'file': (os.path.basename(wav_path), f, 'audio/wav')}
            data = {
                'lang': ai_lang,
                'batch_size': 16,
                'max_length': 256,
            }

            print(f"[Processor] 📤 Sending to AI Server: {AI_SERVER_URL}")
            print(f"[Processor]    └─ POST multipart/form-data")
            print(f"[Processor]    └─ file: {os.path.basename(wav_path)} ({upload_size:,} bytes, audio/wav)")
            print(f"[Processor]    └─ form fields: {data}")
            t1 = time.time()
            response = requests.post(AI_SERVER_URL, files=files, data=data, timeout=600) # 10 min timeout

        elapsed = time.time() - t1
        print(f"[Processor] ⬅️  AI Server response: HTTP {response.status_code} in {elapsed:.1f}s ({len(response.content):,} bytes)")

        if response.status_code != 200:
            # Surface the body so we can see WHY the AI rejected the request
            # (e.g. unsupported lang code, malformed audio, model OOM).
            body_preview = response.text[:1000]
            print(f"[Processor] ❌ AI Server Error body: {body_preview}")
            return None

        ai_data = response.json()
        segments = ai_data.get('segments', [])
        # Log the top-level response keys (without dumping all segments)
        meta = {k: v for k, v in ai_data.items() if k != 'segments'}
        print(f"[Processor]    └─ response meta: {meta}")
        print(f"[Processor]    └─ segments: {len(segments)}")

        # Drift check: compare AI's max segment.end to actual audio duration.
        # Probe BOTH the source webm AND the converted wav with ffprobe so we
        # can spot any timing change introduced by the conversion itself.
        try:
            webm_secs = probe_duration(file_path)
            wav_secs_probe = probe_duration(wav_path)
            wav_secs_size = max((os.path.getsize(wav_path) - 44) / 32000.0, 0.0)
            max_end = max((float(s.get('end', 0)) for s in segments), default=0.0)
            print(
                f"[Processor] 🕒 webm dur: {webm_secs:.2f}s | "
                f"wav dur (probe): {wav_secs_probe:.2f}s | "
                f"wav dur (size): {wav_secs_size:.2f}s | "
                f"AI max end: {max_end:.2f}s"
                if webm_secs is not None and wav_secs_probe is not None
                else f"[Processor] 🕒 wav dur: {wav_secs_size:.2f}s | AI max end: {max_end:.2f}s"
            )
            if webm_secs and wav_secs_probe:
                webm_wav_diff = wav_secs_probe - webm_secs
                ai_wav_diff = max_end - wav_secs_probe
                print(
                    f"[Processor]    └─ wav-vs-webm: {webm_wav_diff:+.3f}s "
                    f"({webm_wav_diff/webm_secs*100 if webm_secs else 0:+.3f}%) "
                    f"| AI-vs-wav: {ai_wav_diff:+.2f}s"
                )
        except Exception as e:
            print(f"[Processor] 🕒 drift check skipped: {e}")
        if segments:
            sample = segments[0]
            print(f"[Processor]    └─ sample[0] (raw): {json.dumps(sample, ensure_ascii=False)[:300]}")
        else:
            print(f"[Processor] ⚠️  AI returned 200 but zero segments. Full body: {json.dumps(ai_data, ensure_ascii=False)[:500]}")

        # AI embeds "__<lang>__" as a marker (e.g. "__ps__ السلام..."). Strip
        # every occurrence regardless of position. Use the AI-side lang code
        # because the marker reflects what we sent it.
        clean_segments(segments, ai_lang)
        if segments:
            print(f"[Processor]    └─ sample[0] (cleaned): {json.dumps(segments[0], ensure_ascii=False)[:300]}")

        # Step 2: Save to local DB for caching
        print(f"[Processor] 💾 Saving results to local database...")
        t2 = time.time()
        db = db_session_factory()
        try:
            video = db.query(database.Video).filter(database.Video.youtube_id == video_id).first()
            if not video:
                print(f"[Processor] ❌ Video {video_id} not found in DB!")
                return None

            # Delete old transcriptions if any to avoid duplicates
            db.query(database.Transcription).filter(
                database.Transcription.video_id == video.id,
                database.Transcription.language == lang
            ).delete()

            new_transcription = database.Transcription(
                video_id=video.id,
                language=lang,
                segments_json=segments
            )
            db.add(new_transcription)
            db.commit()
            print(f"[Processor] ✅ Saved to DB in {time.time() - t2:.1f}s")
        finally:
            db.close()

        return segments

    except Exception as e:
        print(f"[Processor] ❌ Network/Processing Error: {e}")
        return None

    finally:
        total = time.time() - overall_start
        print(f"[Processor] ========== AI Job complete in {total:.1f}s total ==========\n")

        # Cleanup both the original webm and the converted wav
        for p in (file_path, wav_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                    print(f"[Processor] 🗑️  Cleaned up: {os.path.basename(p)}")
                except Exception as cleanup_err:
                    print(f"[Processor] ⚠️  Cleanup failed for {p}: {cleanup_err}")
