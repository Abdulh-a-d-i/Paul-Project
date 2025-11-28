# utils.py - AWS S3 VERSION

from fastapi import Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError
from src.utils.jwt_utils import decode_access_token
import logging
import os
import json
import traceback
from botocore.config import Config

from datetime import datetime, timezone, timedelta
from livekit import api

# AWS imports (replacing GCS)
import boto3
from botocore.exceptions import ClientError

from src.utils.db import PGDB

db = PGDB()
auth_scheme = HTTPBearer()


def get_s3_client():
    """Initialize AWS S3 client"""
    aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    aws_region = os.getenv("AWS_REGION", "us-east-2")
    
    if not aws_access_key or not aws_secret_key:
        raise RuntimeError("Missing AWS credentials")
    
    return boto3.client(
        's3',
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        region_name=aws_region
    )


def get_current_user(token: HTTPAuthorizationCredentials = Depends(auth_scheme)):
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
    """Check if the current user is an admin."""
    try:
        if not current_user.get("is_admin", False):
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to perform this action."
            )
    except Exception as e:
        logging.error(f"Error checking admin status: {e}")
        raise HTTPException(status_code=500, detail=f"{e}")
    return current_user


def add_call_event(call_id: str, event_type: str, event_data: dict = None):
    """Store event in call_history.events_log (deduplicated)"""
    try:
        with db.conn() as (conn, cursor):
            cursor.execute("SELECT events_log FROM call_history WHERE call_id = %s", (call_id,))
            row = cursor.fetchone()
            
            if not row:
                logging.warning(f"Call {call_id} not found for event {event_type}")
                return

            events_log = row.get("events_log") if isinstance(row, dict) else row[0]
            events_log = events_log or []
            
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
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": event_data or {}
            })

            cursor.execute(
                "UPDATE call_history SET events_log = %s WHERE call_id = %s",
                (json.dumps(events_log), call_id)
            )
            conn.commit()
            
    except Exception as e:
        logging.error(f"Error adding call event: {e}")
        traceback.print_exc()


async def get_livekit_call_status(call_id: str):
    """Get current status from LiveKit API"""
    try:
        lkapi = api.LiveKitAPI(
            url=os.getenv("LIVEKIT_URL", "").replace("wss://", "https://"),
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET"),
        )
        
        room_info = await lkapi.room.list_rooms(api.ListRoomsRequest())
        room_exists = any(room.name == call_id for room in room_info.rooms)
        
        await lkapi.aclose()
        
        if room_exists:
            return {"status": "active", "message": "Call is in progress"}
        else:
            return {"status": "ended", "message": "Room not found"}
            
    except Exception as e:
        logging.error(f"Error checking LiveKit: {e}")
        return {"status": "unknown", "error": str(e)}


async def fetch_and_store_transcript(call_id: str, transcript_url: str = None, transcript_blob: str = None):
    """Download transcript from S3 and store in DB"""
    try:
        transcript_data = None
        
        if transcript_blob:
            logging.info(f"📥 Downloading transcript from S3: {transcript_blob}")
            try:
                s3 = get_s3_client()
                bucket_name = os.getenv("AWS_S3_BUCKET_NAME")
                
                response = s3.get_object(Bucket=bucket_name, Key=transcript_blob)
                transcript_json = response['Body'].read().decode('utf-8')
                transcript_data = json.loads(transcript_json)
                
                logging.info(f"✅ Downloaded transcript from S3")
                
            except ClientError as e:
                if e.response['Error']['Code'] == 'NoSuchKey':
                    logging.error(f"❌ S3 key not found: {transcript_blob}")
                else:
                    logging.error(f"❌ S3 error: {e}")
                return None
            except Exception as e:
                logging.error(f"❌ Download failed: {e}")
                traceback.print_exc()
                return None
        else:
            logging.warning(f"⚠️ No transcript_blob for {call_id}")
            return None
        
        if transcript_data:
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
        
        return None
        
    except Exception as e:
        logging.error(f"❌ Error fetching transcript: {e}")
        traceback.print_exc()
        return None


async def fetch_and_store_recording(call_id: str, recording_url: str = None, recording_blob_name: str = None):
    """
    Recording stays in S3 - we just verify it exists.
    NO DOWNLOAD NEEDED!
    """
    try:
        logging.info(f"🎵 Verifying recording exists for {call_id}")
        
        # Get blob name from DB if not provided
        if not recording_blob_name:
            with db.conn() as (conn, cursor):
                cursor.execute("""
                    SELECT recording_blob
                    FROM call_history
                    WHERE call_id = %s
                """, (call_id,))
                row = cursor.fetchone()
                if row:
                    recording_blob_name = row.get("recording_blob") if isinstance(row, dict) else row[0]
        
        if not recording_blob_name:
            logging.warning(f"⚠️ No recording blob for {call_id}")
            return
        
        # Just verify it exists in S3
        s3_client = get_s3_client()
        bucket_name = os.getenv("AWS_S3_BUCKET_NAME")
        
        try:
            s3_client.head_object(Bucket=bucket_name, Key=recording_blob_name)
            logging.info(f"✅ Recording exists in S3: {recording_blob_name}")
        except ClientError:
            logging.warning(f"⚠️ Recording not found in bucket: {recording_blob_name}")
            
    except Exception as e:
        logging.error(f"❌ Error verifying recording: {e}")
        traceback.print_exc()



def generate_presigned_url(s3_key: str, expiration: int = 3600) -> str:
    """
    Generate presigned URL for S3 object using Signature Version 4.
    """
    try:
        bucket_name = os.getenv("AWS_S3_BUCKET_NAME")
        
        # 🔍 Debug: Check what credentials are being loaded
        import boto3
        session = boto3.Session(
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_REGION", "us-east-2")
        )
        
        credentials = session.get_credentials()
        logging.info(f"🔑 Access Key: {credentials.access_key}")
        logging.info(f"🔑 Secret Key (first 5): {credentials.secret_key[:5]}...")
        logging.info(f"🔑 Token: {credentials.token}")  # Should be None for IAM users
        
        s3_client = session.client('s3', config=Config(signature_version='s3v4'))
        
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={
                'Bucket': bucket_name,
                'Key': s3_key
            },
            ExpiresIn=expiration
        )
        
        logging.info(f"✅ Generated presigned URL: {url[:100]}...")
        return url
        
    except Exception as e:
        logging.error(f"❌ Failed to generate presigned URL for {s3_key}: {e}")
        traceback.print_exc()
        return None

def calculate_duration(started_at, ended_at) -> float:
    """Calculate call duration in seconds"""
    if not started_at or not ended_at:
        logging.warning(f"⚠️ Missing timestamps")
        return 0
    
    try:
        if isinstance(started_at, (int, float)):
            start_dt = datetime.fromtimestamp(started_at, tz=timezone.utc)
        elif isinstance(started_at, str):
            start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        elif isinstance(started_at, datetime):
            start_dt = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
        else:
            return 0
        
        if isinstance(ended_at, (int, float)):
            end_dt = datetime.fromtimestamp(ended_at, tz=timezone.utc)
        elif isinstance(ended_at, str):
            end_dt = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        elif isinstance(ended_at, datetime):
            end_dt = ended_at if ended_at.tzinfo else ended_at.replace(tzinfo=timezone.utc)
        else:
            return 0
        
        duration = (end_dt - start_dt).total_seconds()
        return round(max(0, duration), 1)
        
    except Exception as e:
        logging.error(f"❌ Duration calculation error: {e}")
        return 0


def check_if_answered(events_log) -> bool:
    """Determine if call was answered"""
    if not events_log:
        return False
    
    try:
        events = json.loads(events_log) if isinstance(events_log, str) else events_log
        
        egress_started = any(ev.get("event") == "egress_started" for ev in events)
        
        sip_participant_joined = False
        for ev in events:
            if ev.get("event") == "participant_joined":
                participant = ev.get("data", {}).get("participant", {})
                identity = participant.get("identity", "")
                if identity.startswith("sip-"):
                    sip_participant_joined = True
                    break
        
        answered = egress_started or sip_participant_joined
        return answered
        
    except Exception as e:
        logging.error(f"❌ Error parsing events: {e}")
        return False