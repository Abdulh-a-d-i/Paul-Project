from fastapi import Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError
from src.utils.jwt_utils import decode_access_token
import logging
import os  # ✅ ADD THIS - needed for os.getenv()
import json
import base64
import httpx
import traceback
from datetime import datetime, timezone  # ✅ Make sure timezone is imported
from livekit import api

# GCS imports
from google.cloud import storage
from google.cloud.exceptions import NotFound
from google.oauth2 import service_account

from src.utils.db import PGDB

db = PGDB()
auth_scheme = HTTPBearer()
# oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login")

from fastapi.security import HTTPBearer,HTTPAuthorizationCredentials
auth_scheme = HTTPBearer()

def get_gcs_client():
    """Initialize GCS client with service account"""
    gcp_key_b64 = os.getenv("GCS_SERVICE_ACCOUNT_KEY") or os.getenv("GCP_SERVICE_ACCOUNT_KEY_BASE64")
    if not gcp_key_b64:
        raise RuntimeError("GCS_SERVICE_ACCOUNT_KEY not set")
    
    decoded = base64.b64decode(gcp_key_b64).decode("utf-8")
    key_json = json.loads(decoded)
    credentials = service_account.Credentials.from_service_account_info(key_json)
    return storage.Client(credentials=credentials, project=key_json.get("project_id"))


def get_current_user(token: HTTPAuthorizationCredentials = Depends(auth_scheme)):
    # Token decode step
    try:
        logging.info(f"token: {token}")
        payload = decode_access_token(token.credentials)
        if not payload or "sub" not in payload:
            logging.warning("JWT decode failed or missing 'sub' claim.")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except Exception as e:
        logging.error(f"JWT decode error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = int(payload["sub"])
    # DB lookup step
    try:
        user = db.get_user_by_id(user_id)
        if not user:
            logging.warning(f"User not found in DB for user_id: {user_id}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found.",
            )
        return user
    except Exception as e:
        logging.error(f"Database error while fetching user_id {user_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error while fetching user"
        )

def error_response(message: str, status_code: int = 400):
    return JSONResponse(
        status_code=status_code,
        content={"error": message}
    )



def is_admin(current_user=Depends(get_current_user)):
    """
    Check if the current user is an admin.
    If not, return a 403 Forbidden response.
    """
    # # Assuming current_user[5] is the admin flag (True/False)
    print(current_user)
    try:
        if current_user[5] == False:
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to perform this action."
            )
    except Exception as e:
        logging.error(f"Error checking admin status for user : {e}")
        raise HTTPException(
            status_code=500,
            detail=f"{e}"
        )

    return current_user

def add_call_event(call_id: str, event_type: str, event_data: dict = None):
    """Store event in call_history.events_log (deduplicated)"""
    conn = db.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT events_log FROM call_history WHERE call_id = %s", (call_id,))
            row = cursor.fetchone()
            if not row:
                logging.warning(f"Call {call_id} not found for event {event_type}")
                return

            events_log = row[0] or []
            if isinstance(events_log, str):
                try:
                    events_log = json.loads(events_log)
                except Exception:
                    events_log = []

            if any(ev.get("event") == event_type for ev in events_log):
                logging.info(f"Duplicate event {event_type} ignored for {call_id}")
                return

            events_log.append({
                "event": event_type,
                "timestamp": datetime.utcnow().isoformat(),
                "data": event_data or {}
            })

            cursor.execute(
                "UPDATE call_history SET events_log = %s WHERE call_id = %s",
                (json.dumps(events_log), call_id)
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"Error adding call event: {e}")
    finally:
        db.release_connection(conn)  # ✅ 

from livekit import api
import os
import asyncio
from dotenv import load_dotenv


LIVEKIT_API_URL = os.getenv("LIVEKIT_URL", "").replace("wss://", "https://")

async def get_livekit_call_status(call_id: str):
    """
    Get current status from LiveKit API
    """
    try:
        lkapi = api.LiveKitAPI(
            url=os.getenv("LIVEKIT_URL", "").replace("wss://", "https://"),
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET"),
        )
        
        room_info = await lkapi.room.list_rooms(api.ListRoomsRequest())
        
        room_exists = any(room.name == call_id for room in room_info.rooms)
        
        logging.info(f"🔍 LiveKit Check: Room {call_id} exists = {room_exists}")
        
        await lkapi.aclose()
        
        if room_exists:
            return {
                "status": "active",
                "message": "Call is in progress"
            }
        else:
            return {
                "status": "ended",
                "message": "Room not found in LiveKit"
            }
            
    except Exception as e:
        logging.error(f"Error checking LiveKit status: {e}")
        return {
            "status": "unknown",
            "error": str(e)
        }


import traceback

async def fetch_and_store_transcript(call_id: str, transcript_url: str = None, transcript_blob: str = None):
    """
    Download transcript from GCS blob ONLY (never use signed URLs).
    Signed URLs cause timeouts and ReadErrors.
    """
    try:
        transcript_data = None
        
        # ✅ ONLY use GCS blob (direct access with service account)
        if transcript_blob:
            logging.info(f"📥 Downloading transcript from blob: {transcript_blob}")
            try:
                gcs = get_gcs_client()
                bucket_name = os.getenv("GOOGLE_BUCKET_NAME")
                bucket = gcs.bucket(bucket_name)
                blob = bucket.blob(transcript_blob)
                
                if blob.exists():
                    transcript_json = blob.download_as_text()
                    transcript_data = json.loads(transcript_json)
                    logging.info(f"✅ Downloaded transcript from blob")
                else:
                    logging.error(f"❌ Blob not found: {transcript_blob}")
            except Exception as e:
                logging.error(f"❌ Blob download failed: {e}")
                traceback.print_exc()
                return None
        else:
            logging.warning(f"⚠️ No transcript_blob provided for {call_id}")
            return None
        
        # Store in database
        if transcript_data:
            # Check if has content
            has_content = False
            if isinstance(transcript_data, dict):
                items = transcript_data.get("items") or transcript_data.get("messages") or []
                has_content = len(items) > 0
            elif isinstance(transcript_data, list):
                has_content = len(transcript_data) > 0
            
            if has_content:
                db.update_call_history(call_id, {"transcript": transcript_data})
                logging.info(f"✅ Transcript stored ({len(str(transcript_data))} chars)")
            else:
                logging.warning(f"⚠️ Empty transcript for {call_id}")
                db.update_call_history(call_id, {"transcript": {"items": [], "note": "No conversation"}})
            
            return transcript_data
        
        logging.warning(f"⚠️ No transcript data for {call_id}")
        return None
        
    except Exception as e:
        logging.error(f"❌ Error fetching transcript: {e}")
        traceback.print_exc()
        return None


async def fetch_and_store_recording(call_id: str, recording_url: str = None, recording_blob_name: str = None):
    """Download recording and store BYTES in database"""
    try:
        logging.info(f"🎵 Fetching recording for call {call_id}")
        
        # Get blob name from DB if not provided
        if not recording_blob_name:
            conn = db.get_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT recording_blob
                        FROM call_history
                        WHERE call_id = %s
                    """, (call_id,))
                    row = cursor.fetchone()
                    if row:
                        recording_blob_name = row[0]
            finally:
                db.release_connection(conn)
        
        if not recording_blob_name:
            logging.warning(f"⚠️ No recording blob for {call_id}")
            return
        
        # ✅ Download from GCS blob ONLY
        recording_data = await _fetch_from_gcs_blob(recording_blob_name)
        
        if recording_data:
            # ✅ Store in database
            db.store_recording_blob(
                call_id=call_id,
                recording_data=recording_data,
                content_type="audio/ogg"
            )
            logging.info(f"✅ Stored {len(recording_data)} bytes for {call_id}")
        else:
            logging.error(f"❌ Failed to download recording for {call_id}")
            
    except Exception as e:
        logging.error(f"❌ Error fetching recording: {e}")
        traceback.print_exc()


async def _fetch_from_gcs_blob(blob_name: str) -> bytes:
    """Download file from GCS using blob name"""
    try:
        gcs = get_gcs_client()  # ← This already handles base64 decoding
        bucket_name = os.getenv("GOOGLE_BUCKET_NAME")
        bucket = gcs.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        
        if blob.exists():
            data = blob.download_as_bytes()
            logging.info(f"✅ Downloaded {len(data)} bytes from GCS: {blob_name}")
            return data
        else:
            logging.error(f"❌ Blob not found in GCS: {blob_name}")
            return None
            
    except Exception as e:
        logging.error(f"❌ GCS download failed for {blob_name}: {e}")
        traceback.print_exc()
        return None

async def _fetch_from_url(url: str) -> bytes:
    """
    Download the ACTUAL AUDIO FILE from HTTP URL.
    Returns: Raw audio bytes (MP3/OGG file content)
    """
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(url)
            
            if response.status_code == 200:
                # ✅ This is the ACTUAL AUDIO FILE content
                recording_bytes = response.content
                logging.info(f"✅ Downloaded {len(recording_bytes)} bytes of AUDIO from URL")
                return recording_bytes
            else:
                logging.error(f"❌ Failed to download: HTTP {response.status_code}")
                return None
                
    except Exception as e:
        logging.error(f"❌ Failed to download from URL: {e}")
        return None




# ============================================
# ✅ HELPER FUNCTIONS
# ============================================

def calculate_duration(started_at, ended_at) -> float:
    """
    Calculate call duration in seconds from timestamps.
    Handles None values, various timestamp formats, and timezone issues.
    """
    if not started_at or not ended_at:
        logging.warning(f"⚠️ Missing timestamps: start={started_at}, end={ended_at}")
        return 0
    
    try:
        # Convert to datetime objects if needed
        if isinstance(started_at, (int, float)):
            start_dt = datetime.fromtimestamp(started_at, tz=timezone.utc)
        elif isinstance(started_at, str):
            start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        elif isinstance(started_at, datetime):
            start_dt = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
        else:
            logging.error(f"❌ Invalid started_at type: {type(started_at)}")
            return 0
        
        if isinstance(ended_at, (int, float)):
            end_dt = datetime.fromtimestamp(ended_at, tz=timezone.utc)
        elif isinstance(ended_at, str):
            end_dt = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        elif isinstance(ended_at, datetime):
            end_dt = ended_at if ended_at.tzinfo else ended_at.replace(tzinfo=timezone.utc)
        else:
            logging.error(f"❌ Invalid ended_at type: {type(ended_at)}")
            return 0
        
        # Calculate duration
        duration = (end_dt - start_dt).total_seconds()
        
        # Sanity check
        if duration < 0:
            logging.warning(f"⚠️ Negative duration: {duration}s (end before start)")
            return 0
        
        if duration > 86400:  # More than 24 hours
            logging.warning(f"⚠️ Suspiciously long duration: {duration}s")
        
        return round(max(0, duration), 1)
        
    except Exception as e:
        logging.error(f"❌ Error calculating duration: {e}")
        logging.error(f"   started_at: {started_at} ({type(started_at)})")
        logging.error(f"   ended_at: {ended_at} ({type(ended_at)})")
        traceback.print_exc()
        return 0
    
    
def check_if_answered(events_log) -> bool:
    """
    Determine if call was actually answered by checking events_log.
    
    ⚠️ CRITICAL: We can ONLY check events_log because transcript 
    doesn't exist yet when room_ended fires!
    
    Returns True if:
    - SIP participant joined (means they picked up)
    - Recording started (egress_started means call was answered)
    """
    if not events_log:
        logging.warning("⚠️ No events_log - assuming unanswered")
        return False
    
    try:
        events = json.loads(events_log) if isinstance(events_log, str) else events_log
        
        # ✅ Check if recording started (definitive proof)
        egress_started = any(ev.get("event") == "egress_started" for ev in events)
        
        # ✅ Check if SIP participant joined (they picked up)
        sip_participant_joined = False
        for ev in events:
            if ev.get("event") == "participant_joined":
                participant = ev.get("data", {}).get("participant", {})
                identity = participant.get("identity", "")
                if identity.startswith("sip-"):
                    sip_participant_joined = True
                    break
        
        # ✅ Either condition means call was answered
        answered = egress_started or sip_participant_joined
        
        logging.info(f"📊 Answered check: egress={egress_started}, sip_joined={sip_participant_joined} → {answered}")
        
        return answered
        
    except Exception as e:
        logging.error(f"❌ Error parsing events_log: {e}")
        return False
    



