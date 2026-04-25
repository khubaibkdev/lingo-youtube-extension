import os
import json
import time
import requests
from sqlalchemy.orm import Session
import database

AI_SERVER_URL = "http://192.168.18.119:8004/translate-audio"

def process_audio(file_path: str, video_id: str, lang: str, db_session_factory):
    """
    Sends audio to the external AI server for transcription/translation and saves result to DB.
    """
    overall_start = time.time()
    print(f"\n[Processor] ========== Forwarding to AI Server: {video_id} ({lang}) ==========")

    try:
        # Step 1: Prepare files and data for external API
        if not os.path.exists(file_path):
            print(f"[Processor] ❌ File not found: {file_path}")
            return

        with open(file_path, 'rb') as f:
            files = {'file': (os.path.basename(file_path), f, 'audio/webm')}
            data = {
                'lang': lang,         # 'ps' or 'bal'
                'batch_size': 16,     # Default batch size
                'max_length': 256     # Default max length
            }

            print(f"[Processor] 📤 Sending to AI Server: {AI_SERVER_URL}...")
            t1 = time.time()
            response = requests.post(AI_SERVER_URL, files=files, data=data, timeout=600) # 10 min timeout
            
        if response.status_code != 200:
            print(f"[Processor] ❌ AI Server Error ({response.status_code}): {response.text}")
            return

        ai_data = response.json()
        segments = ai_data.get('segments', [])
        print(f"[Processor] ✅ AI Server returned {len(segments)} segments in {time.time() - t1:.1f}s")

        # Step 2: Save to local DB for caching
        print(f"[Processor] 💾 Saving results to local database...")
        t2 = time.time()
        db = db_session_factory()
        try:
            video = db.query(database.Video).filter(database.Video.youtube_id == video_id).first()
            if not video:
                print(f"[Processor] ❌ Video {video_id} not found in DB!")
                return

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

    except Exception as e:
        print(f"[Processor] ❌ Network/Processing Error: {e}")

    finally:
        total = time.time() - overall_start
        print(f"[Processor] ========== AI Job complete in {total:.1f}s total ==========\n")
        
        # Cleanup temp file after processing is done
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                print(f"[Processor] 🗑️  Cleaned up temp file: {file_path}")
        except:
            pass
