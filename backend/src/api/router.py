import json
import logging
import os
import io

import traceback
from datetime import datetime, timedelta,timezone
from typing import Dict, List, Optional, Tuple, Any
import requests
import asyncio
from dotenv import load_dotenv
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
)
from datetime import datetime

from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse,StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import HTTPException, Response
from rich import print
from src.api.base_models import (
    UserLogin,
    UserRegister,
    UserOut,
    LoginResponse,
    UpdateUserProfileRequest,
    Assistant_Payload,
    PromptCustomizationUpdate
)
from src.models.System_Prompt import SystemPromptBuilder
from src.utils.db import PGDB 
from src.utils.mail_management import Send_Mail
from src.utils.jwt_utils import create_access_token
from src.utils.utils import get_current_user,add_call_event, get_livekit_call_status,fetch_and_store_transcript,fetch_and_store_recording, calculate_duration, check_if_answered
from livekit import api

load_dotenv()

router = APIRouter()
mail_obj = Send_Mail()
db = PGDB()
load_dotenv(override=True)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GCS_BUCKET_NAME = os.getenv("GOOGLE_BUCKET_NAME")
GCS_SERVICE_ACCOUNT_KEY = os.getenv("GCS_SERVICE_ACCOUNT_KEY")  


# error response 
def error_response(message, status_code=400):
    return JSONResponse(
        status_code=status_code,
        content={"error": message}
    )


@router.post("/register")
def register_user(user: UserRegister):
    user_dict = user.dict()
    #  Normalize both email and username
    user_dict["email"] = user_dict["email"].strip().lower()
    user_dict["username"] = user_dict["username"].strip().lower()
    user_dict['is_admin'] = True
    try:
        db.register_user(user_dict)
        return JSONResponse(status_code=201, content={"message": "You are registered successfully."})
    except ValueError as ve:
        return error_response(status_code=400, message=str(ve))
    except Exception as e:
        traceback.print_exc()
        return error_response(status_code=500, message=f"Registration failed: {str(e)}")

@router.post("/login",response_model=LoginResponse,)
def login_user(user: UserLogin):
    try:
        user_dict = {
        "email": user.email,
        "password": user.password
    }
        logging.info(f"User dict: {user_dict}")
        user_dict["email"] = user_dict["email"].strip().lower()
        result = db.login_user(user_dict)
        if not result:
            return error_response("Invalid username or password", status_code=422)
        
        
        token = create_access_token({"sub": str(result["id"])})
        return {
            "access_token": token,
            "token_type": "bearer",
            "user": result
        }
        
    except ValueError as ve:
        # Return 401 when credentials are invalid
        return error_response(str(ve),status_code=422)

    except Exception as e:
        logging.error(f"Error during login: {str(e)}")
        return error_response(f"Internal server error: {str(e)}",status_code=500)
    


voices = {
    # English voices
    "david": "1SM7GgM6IMuvQlz2BwM3",
    "ravi": "A7AUsa1uITCDpK29MG3m",
    "emily-british": "9YWmufCrZ2agGoSoVL8je",
    "alice-british": "XcXEQzuLXRU9RcfWzEJt",
    "julia-british": "ZtcPZrt9K4w8e1OB9M6w",
    
    # Spanish voices
    "julio": "A7AUsa1uITCDpK29MG3m",
    "donato": "851ejYcv2BoNPjrkw93G",
    "helena-spanish": "5vkxOzoz40FrElmLP4P7",
    "rosa": "BIvP0GN1cAtSRTxNHnWS",
    "mariam": "90ipbRoKi4CpHXvKVtl0",
}

@router.post("/assistant-initiate-call")
async def make_call_with_livekit(payload: Assistant_Payload, user=Depends(get_current_user)):
    try:
        room_name = f"call-{user['id']}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        #  Get voice_id from payload.voice name
        voice_name = getattr(payload, "voice", "david").lower()  # Default to 'david'
        voice_id = voices.get(voice_name)
        
        if not voice_id:
            logging.warning(f" Unknown voice '{voice_name}', using default 'david'")
            voice_id = voices["david"]
            voice_name = "david"
        
        #  Get language from payload (default to 'en')
        language = getattr(payload, "language", "en").lower()
        if language not in ["en", "es"]:
            logging.warning(f" Unknown language '{language}', defaulting to 'en'")
            language = "en"
        
        logging.info(f" Using voice: {voice_name} (ID: {voice_id}), Language: {language}")
        
        #  STEP 1: Get user's custom prompt from DB
        user_prompt_data = db.get_user_prompt(user["id"])
        
        if not user_prompt_data:
            return error_response("User prompt not found", status_code=404)
        
        base_prompt = user_prompt_data["system_prompt"]
        
        #  STEP 2: Build complete system prompt
        prompt_builder = SystemPromptBuilder(
            base_prompt=base_prompt,
            caller_name=payload.caller_name,
            caller_email=payload.caller_email,
            call_context=payload.context,
            language=language  

        )
        
        complete_system_prompt = prompt_builder.generate_complete_prompt()
        
        logging.info(f" Built system prompt ({len(complete_system_prompt)} chars)")
        
        #  STEP 3: Prepare metadata with complete prompt + voice + language
        metadata = {
            "phone_number": payload.outbound_number,
            "call_context": payload.context,
            "user_id": user["id"],
            "caller_name": payload.caller_name,
            "caller_email": payload.caller_email,
            "system_prompt": complete_system_prompt,
            "agent_name": "SUMA",
            "voice_id": voice_id,        
            "voice_name": voice_name,    
            "language": language         
        }

        #  STEP 4: Create DB record
        db.insert_call_history(
            user_id=user["id"],
            call_id=room_name,
            status="initiated",
            to_number=payload.outbound_number,
            voice_name=voice_name,  
        )
        logging.info(f" Created call record: {room_name}")

        add_call_event(room_name, "call_initiated", {"user_id": user["id"]})

        async with api.LiveKitAPI(
            url=os.getenv("LIVEKIT_URL", "").replace("wss://", "https://"),
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET"),
        ) as lkapi:
            dispatch = await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name="outbound-caller",
                    room=room_name,
                    metadata=json.dumps(metadata),
                )
            )

        logging.info(f" Agent dispatched: {dispatch.id}")

        return JSONResponse({
            "success": True,
            "call_id": room_name,
            "dispatch_id": dispatch.id,
            "voice": voice_name,
            "language": language,
            "message": "Call initiated successfully"
        })
        
    except Exception as e:
        logging.error(f"Error initiating LiveKit call: {e}")
        traceback.print_exc()
        
        if 'room_name' in locals():
            try:
                db.update_call_history(
                    call_id=room_name,
                    updates={"status": "failed"}
                )
            except:
                pass
        
        raise HTTPException(status_code=500, detail=f"Failed to initiate call: {str(e)}")


@router.post("/livekit-webhook")
async def livekit_webhook(request: Request):
    try:
        data = await request.json()
        event = data.get("event")
        room = data.get("room", {})
        call_id = room.get("name")

        #  Extract call_id from egress events
        if not call_id:
            egress_info = data.get("egress_info", {}) or data.get("egressInfo", {})
            call_id = egress_info.get("room_name") or egress_info.get("roomName")
            if not call_id:
                return JSONResponse({"message": "No call_id"})

        #  Always log event
        add_call_event(call_id, event, data)
        
        #  Ignore non-critical events
        if event in ["room_started", "participant_joined", "egress_started", 
                     "egress_updated", "track_published", "track_unpublished"]:
            return JSONResponse({"message": f"{event} logged"})

        #  Handle room end
        if event in ["room_finished", "participant_left"]:
            await asyncio.sleep(0.5)
            
            conn = db.get_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT status, events_log, started_at, created_at
                        FROM call_history WHERE call_id = %s
                    """, (call_id,))
                    row = cursor.fetchone()
            finally:
                db.release_connection(conn)  #  FIXED: Changed from conn.close()

            if not row:
                return JSONResponse({"message": "Call not found"})

            current_status, events_log, db_started_at, created_at = row
            
            #  Skip if already final
            if current_status in {"completed", "unanswered"}:
                # Just update duration
                started = db_started_at or created_at
                ended = datetime.now(timezone.utc)
                duration = (ended - started).total_seconds() if started else 0
                
                db.update_call_history(call_id, {
                    "duration": max(0, duration),
                    "ended_at": ended
                })
                return JSONResponse({"message": "Duration updated"})

            #  Determine final status
            answered = check_if_answered(events_log)
            final_status = "completed" if answered else "unanswered"
            
            started = db_started_at or created_at
            ended = datetime.now(timezone.utc)
            duration = (ended - started).total_seconds() if (answered and started) else 0

            db.update_call_history(call_id, {
                "status": final_status,
                "duration": max(0, duration),
                "ended_at": ended,
                "started_at": started
            })
            
            return JSONResponse({"message": f"Call ended: {final_status}"})

        #  Handle recording
        elif event == "egress_ended":
            egress_info = data.get("egress_info", {}) or data.get("egressInfo", {})
            file_results = egress_info.get("file_results", []) or egress_info.get("fileResults", [])
            
            if file_results:
                file_info = file_results[0] if isinstance(file_results, list) else file_results
                location = file_info.get("location") or file_info.get("download_url")
                
                if location:
                    db.update_call_history(call_id, {"recording_url": location})
                    return JSONResponse({"message": "Recording saved"})

        return JSONResponse({"message": f"{event} processed"})

    except Exception as e:
        logging.error(f"Webhook error: {e}")
        traceback.print_exc()  #  Added for better debugging
        return JSONResponse({"error": str(e)}, status_code=500)



    


@router.post("/livekit-egress-webhook")
async def livekit_egress_webhook(request: Request):
    """Alias endpoint for egress-specific webhooks"""
    return await livekit_webhook(request)


# @router.get("/call-history")
# async def get_user_call_history(
#     page: int = Query(1, ge=1),
#     page_size: int = Query(10, ge=1, le=100),
#     user = Depends(get_current_user)
# ):
#     """
#     Get call history with parsed transcripts showing only the conversation text
#     """
#     try:
#         call_history = db.get_call_history_by_user_id(user["id"], page, page_size)
        
#         # Process each call to include formatted transcript
#         processed_calls = []
#         for call in call_history["calls"]:
#             call_data = {**call}
            
#             # Parse and extract transcript text
#             transcript_text = None
#             if call.get("transcript"):
#                 try:
#                     transcript_data = call["transcript"]
                    
#                     # If transcript is a string, parse it
#                     if isinstance(transcript_data, str):
#                         transcript_data = json.loads(transcript_data)
                    
#                     # Extract conversation as plain text
#                     conversation_lines = []
#                     if isinstance(transcript_data, list):
#                         for item in transcript_data:
#                             if item.get("type") == "message":
#                                 role = item.get("role", "unknown")
#                                 content = item.get("content", [])
                                
#                                 # Handle content as list or string
#                                 if isinstance(content, list):
#                                     text = " ".join(str(c) for c in content)
#                                 else:
#                                     text = str(content)
                                
#                                 # Format: "Assistant: Hello there"
#                                 speaker = "Assistant" if role == "assistant" else "User"
#                                 conversation_lines.append(f"{speaker}: {text}")
                    
#                     transcript_text = "\n".join(conversation_lines) if conversation_lines else None
                    
#                 except Exception as e:
#                     logging.warning(f"Error parsing transcript for call {call.get('id')}: {e}")
#                     transcript_text = None
            
#             call_data["transcript"] = transcript_text
#             processed_calls.append(call_data)
        
#         return JSONResponse(content=jsonable_encoder({
#             "user_id": user["id"],
#             "pagination": {
#                 "page": call_history["page"],
#                 "page_size": call_history["page_size"],
#                 "total": call_history["total"],
#                 "completed_calls": call_history["completed_calls"],
#                 "not_completed_calls": call_history["not_completed_calls"]
#             },
#             "calls": processed_calls
#         }))
#     except Exception as e:
#         logging.error(f"Error fetching call history: {e}")
#         traceback.print_exc()
#         raise HTTPException(status_code=500, detail=f"Error fetching call history: {str(e)}")


# In routes.py - Update get_call_status endpoint


@router.get("/call-status/{call_id}")
async def get_call_status(call_id: str):
    """Optimized status check with proper connection handling"""
    try:
        conn = db.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT status, created_at, ended_at, duration, started_at
                    FROM call_history 
                    WHERE call_id = %s
                """, (call_id,))
                row = cursor.fetchone()
        finally:
            db.release_connection(conn)  #  FIXED: Was conn.close()
        
        if not row:
            return JSONResponse(
                status_code=404,
                content={"status": "not_found", "is_final": True}
            )
        
        current_status, created_at, ended_at, duration, started_at = row
        
        #  Normalize status
        if current_status not in {"initialized", "dialing", "connected", "completed", "unanswered"}:
            STATUS_MAP = {
                "initiated": "initialized",
                "in_progress": "connected",
                "failed": "unanswered",
                "not_attended": "unanswered"
            }
            current_status = STATUS_MAP.get(current_status, "initialized")
        
        # Calculate elapsed time
        time_elapsed = 0
        if created_at:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            time_elapsed = (datetime.now(timezone.utc) - created_at).total_seconds()
        
        is_final = current_status in {"completed", "unanswered"}
        
        response = {
            "status": current_status,
            "message": {
                "initialized": "Initializing...",
                "dialing": "Dialing...",
                "connected": "Call in progress",
                "completed": "Call completed",
                "unanswered": "Call not answered"
            }.get(current_status, current_status),
            "time_elapsed": round(time_elapsed, 1),
            "is_final": is_final
        }
        
        if is_final and duration:
            response["duration"] = round(duration, 1)
        
        if started_at:
            response["started_at"] = started_at.isoformat()
        if ended_at:
            response["ended_at"] = ended_at.isoformat()
        
        return JSONResponse(response)
        
    except Exception as e:
        logging.error(f"get_call_status error: {e}")
        return JSONResponse(
            {"status": "error", "message": str(e), "is_final": True},
            status_code=500
        )
                            

@router.get("/call-history")
async def get_user_call_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, le=100),
    user=Depends(get_current_user)
):
    try:
        history = db.get_call_history_by_user_id(user["id"], page, page_size)

        calls = []
        for call in history.get("calls", []):
            call_data = {**call}
            
            
            if call.get("created_at"):
                call_data["created_at"] = call["created_at"].isoformat() if hasattr(call["created_at"], 'isoformat') else str(call["created_at"])
            
            if call.get("started_at"):
                call_data["started_at"] = call["started_at"].isoformat() if hasattr(call["started_at"], 'isoformat') else str(call["started_at"])
            
            if call.get("ended_at"):
                call_data["ended_at"] = call["ended_at"].isoformat() if hasattr(call["ended_at"], 'isoformat') else str(call["ended_at"])
            
            #  FIX 2: Calculate display duration if not available
            if not call_data.get("duration") and call.get("started_at") and call.get("ended_at"):
                try:
                    from datetime import datetime
                    start = call["started_at"] if isinstance(call["started_at"], datetime) else datetime.fromisoformat(str(call["started_at"]))
                    end = call["ended_at"] if isinstance(call["ended_at"], datetime) else datetime.fromisoformat(str(call["ended_at"]))
                    call_data["duration"] = round((end - start).total_seconds(), 1)
                except:
                    call_data["duration"] = 0
            
            transcript_text = None
            if call.get("transcript"):
                try:
                    tr = call["transcript"]
                    if isinstance(tr, str):
                        tr = json.loads(tr)
                    if isinstance(tr, list):
                        lines = []
                        for msg in tr:
                            if msg.get("type") == "message":
                                speaker = "Assistant" if msg.get("role") == "assistant" else "User"
                                text = " ".join(msg.get("content", [])) if isinstance(msg.get("content"), list) else str(msg.get("content"))
                                lines.append(f"{speaker}: {text}")
                        transcript_text = "\n".join(lines)
                except Exception as e:
                    logging.warning(f"Transcript parse error for {call.get('id')}: {e}")
            
            call_data["transcript_text"] = transcript_text
            
            #  FIX 3: Add recording availability flag
            call_data["has_recording"] = bool(call.get("recording_url") or call.get("recording_blob_data"))
            
            calls.append(call_data)

        # Build pagination block safely
        pagination = history.get("pagination") or {
            "page": history.get("page", page),
            "page_size": history.get("page_size", page_size),
            "total": history.get("total", len(calls)),
            "completed_calls": history.get("completed_calls", 0),
            "not_completed_calls": history.get("not_completed_calls", 0),
        }

        from fastapi.encoders import jsonable_encoder

        return JSONResponse(content=jsonable_encoder({
            "user_id": user["id"],
            "pagination": pagination,
            "calls": calls
        }))

    except Exception as e:
        logging.error(f"Error fetching history: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agent/get-appointments/{user_id}")
async def get_appointments(user_id: int, from_date: str = None):
    """API for LiveKit agent to get all appointments for checking conflicts"""
    try:
        appointments = db.get_user_appointments(user_id, from_date)
        
        return JSONResponse({
            "success": True,
            "user_id": user_id,
            "appointments": [
                {
                    "id": apt["id"],
                    "date": str(apt["appointment_date"]),
                    "start_time": str(apt["start_time"]),
                    "end_time": str(apt["end_time"]),
                    "attendee_email": apt["attendee_email"],
                    "attendee_name": apt["attendee_name"],
                    "title": apt["title"],
                    "description": apt["description"],
                    "status": apt["status"]
                }
                for apt in appointments
            ]
        })
        
    except Exception as e:
        logging.error(f"Error fetching appointments: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)}
        )


# @router.post("/agent/check-availability")
# async def check_availability(request: Request):
#     """
#     API for LiveKit agent to check if a time slot is available
#     """
#     try:
#         data = await request.json()
        
#         user_id = data.get("user_id")
#         appointment_date = data.get("appointment_date")
#         start_time = data.get("start_time")
#         end_time = data.get("end_time")
        
#         has_conflict = db.check_appointment_conflict(
#             user_id=user_id,
#             appointment_date=appointment_date,
#             start_time=start_time,
#             end_time=end_time
#         )
        
#         return JSONResponse({
#             "success": True,
#             "available": not has_conflict,
#             "message": "Time slot available" if not has_conflict else "Time slot already booked"
#         })
        
#     except Exception as e:
#         logging.error(f"Error checking availability: {e}")
#         return error_response(f"Failed to check availability: {str(e)}", status_code=500)


@router.post("/agent/book-appointment")
async def book_appointment(request: Request):
    """
    API for LiveKit agent to book an appointment
    """
    try:
        data = await request.json()
        
        user_id = data.get("user_id")
        appointment_date = data.get("appointment_date") 
        start_time = data.get("start_time")
        end_time = data.get("end_time")
        attendee_name = data.get("attendee_name", "Valued Customer")
        title = data.get("title", "Appointment")
        description = data.get("description", "")
        organizer_name = data.get("organizer_name")
        organizer_email = data.get("organizer_email")
        
        if not all([user_id, appointment_date, start_time, end_time, organizer_email]):
            return error_response("Missing required fields", status_code=400)
        
        has_conflict = db.check_appointment_conflict(
            user_id=user_id,
            appointment_date=appointment_date,
            start_time=start_time,
            end_time=end_time
        )
        
        if has_conflict:
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "message": "Time slot already booked",
                    "conflict": True
                }
            )
        
        appointment_id = db.create_appointment(
            user_id=user_id,
            appointment_date=appointment_date,
            start_time=start_time,
            end_time=end_time,
            attendee_name=attendee_name,
            attendee_email=organizer_email,
            title=title,
            description=description
        )
        
        email_sent = await mail_obj.send_email_with_calendar_event(
            attendee_email=organizer_email,
            attendee_name=organizer_name,
            appointment_date=appointment_date,
            start_time=start_time,
            end_time=end_time,
            title=title,
            description=description,
            organizer_name=organizer_name,
            organizer_email=organizer_email
        )
        
        return JSONResponse({
            "success": True,
            "appointment_id": appointment_id,
            "email_sent": email_sent,
            "message": "Appointment booked successfully"
        })
        
    except Exception as e:
        logging.error(f"Error booking appointment: {e}")
        return error_response(f"Failed to book appointment: {str(e)}", status_code=500)



@router.post("/agent/save-call-data")
async def save_call_data(request: Request):
    try:
        data = await request.json()
        
        call_id = data.get("call_id")
        transcript_blob = data.get("transcript_blob")
        recording_blob = data.get("recording_blob")
        
        # Save metadata
        updates = {
            "transcript_blob": transcript_blob,
            "recording_blob": recording_blob
        }
        
        db.update_call_history(call_id, updates)
        
        #  DELAYED transcript (5s)
        if transcript_blob:
            async def delayed_transcript():
                await asyncio.sleep(5)
                logging.info(f"📄 Downloading transcript for {call_id}")
                await fetch_and_store_transcript(call_id, None, transcript_blob)
            asyncio.create_task(delayed_transcript())
        
        #  DELAYED recording (15s)
        if recording_blob:
            async def delayed_recording():
                await asyncio.sleep(15)
                logging.info(f"🎵 Downloading recording for {call_id}")
                await fetch_and_store_recording(call_id, None, recording_blob)
            asyncio.create_task(delayed_recording())
        
        return JSONResponse({"success": True})
        
    except Exception as e:
        logging.error(f"❌ save_call_data error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    



@router.post("/agent/report-event")
async def receive_agent_event(request: Request):
    try:
        data = await request.json()
        
        call_id = data.get("call_id")
        status = data.get("status")
        timestamp = data.get("timestamp")
        
        if not call_id or not status:
            return JSONResponse({"error": "Missing data"}, status_code=400)
        
        if status not in {"initialized", "dialing", "connected", "unanswered"}:
            return JSONResponse({"error": "Invalid status"}, status_code=400)
        
        #  Build updates
        updates = {"status": status}
        now = datetime.now(timezone.utc)
        
        #  Set started_at on dialing or connected
        if status in {"dialing", "connected"}:
            conn = db.get_connection()
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT started_at FROM call_history WHERE call_id = %s",
                        (call_id,)
                    )
                    row = cursor.fetchone()
                    if row and not row[0]:
                        updates["started_at"] = now
            finally:
                conn.close()
        
        #  Handle unanswered
        if status == "unanswered":
            updates["ended_at"] = now
            updates["duration"] = 0
        
        db.update_call_history(call_id, updates)
        
        return JSONResponse({"success": True})
        
    except Exception as e:
        logging.error(f"report-event error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    











@router.get("/prompt-customization")
async def get_prompt_customization(user=Depends(get_current_user)):
    """
    Get the user's complete system prompt as plain text.
    No field parsing - returns exactly what's stored.
    """
    try:
        prompt_data = db.get_user_prompt(user["id"])
        
        if not prompt_data:
            return error_response("Prompt not found", status_code=404)
        
        # Just return the system_prompt field directly
        return JSONResponse(content=jsonable_encoder({
            "success": True,
            "system_prompt": prompt_data["system_prompt"],  # Single field from DB
            "updated_at": prompt_data["updated_at"]
        }))
        
    except Exception as e:
        logging.error(f"Error fetching prompt customization: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/prompt-customization")
async def update_prompt_customization(
    customization: PromptCustomizationUpdate,
    user=Depends(get_current_user)
):
    """
    Update user's system prompt.
    Stores exactly what user sends - no parsing.
    """
    try:
        prompt_text = customization.system_prompt.strip()
        
        if not prompt_text:
            return error_response("System prompt cannot be empty", status_code=400)
        
        # Just update the single system_prompt field
        updated_prompt = db.update_user_system_prompt(
            user_id=user["id"],
            system_prompt=prompt_text  # Store as-is
        )
        
        if not updated_prompt:
            return error_response("Failed to update customization", status_code=500)
        
        return JSONResponse(content=jsonable_encoder({
            "success": True,
            "message": "Prompt customization updated successfully",
            "system_prompt": updated_prompt["system_prompt"],
            "updated_at": updated_prompt["updated_at"]
        }))
        
    except Exception as e:
        logging.error(f"Error updating prompt customization: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/prompt-customization/reset")
async def reset_prompt_customization(user=Depends(get_current_user)):
    """
    Reset user's system prompt to default text.
    """
    try:
        reset_prompt = db.reset_user_prompt_to_default(user["id"])
        
        if not reset_prompt:
            return error_response("Failed to reset customization", status_code=500)
        
        return JSONResponse(content=jsonable_encoder({
            "success": True,
            "message": "Prompt customization reset to defaults",
            "system_prompt": reset_prompt["system_prompt"],  # Default text
            "updated_at": reset_prompt["updated_at"]
        }))
        
    except Exception as e:
        logging.error(f"Error resetting prompt customization: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.options("/calls/{call_id}/recording/stream")
async def stream_call_recording_options(call_id: str):
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",  # Or specific domain
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Range, Content-Type, Authorization, Accept",
            "Access-Control-Max-Age": "3600"
        }
    )

@router.get("/calls/{call_id}/recording/stream")
async def stream_call_recording(
    call_id: str, 
    user=Depends(get_current_user),
    request: Request = None
):
    try:
        recording_data, content_type, size = db.get_recording_blob(call_id, user["id"])
        
        if recording_data:
            logging.info(f" Streaming {size} bytes for {call_id}")
            
            cors_headers = {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Range, Content-Type, Authorization",
                "Access-Control-Expose-Headers": "Content-Range, Content-Length, Accept-Ranges",
            }
            
            range_header = request.headers.get("range") if request else None
            
            if range_header:
                try:
                    range_match = range_header.replace("bytes=", "").split("-")
                    start = int(range_match[0]) if range_match[0] else 0
                    end = int(range_match[1]) if len(range_match) > 1 and range_match[1].strip() else size - 1
                    
                    # Ensure valid range
                    start = max(0, start)
                    end = min(end, size - 1)
                    
                    chunk = recording_data[start:end + 1]
                    
                    return Response(
                        content=chunk,
                        status_code=206,
                        media_type=content_type or "audio/ogg",
                        headers={
                            **cors_headers,
                            "Content-Range": f"bytes {start}-{end}/{size}",
                            "Content-Length": str(len(chunk)),
                            "Accept-Ranges": "bytes",
                        }
                    )
                except Exception as e:
                    logging.warning(f"Range parse failed: {e}")
            
            # Full file stream
            return StreamingResponse(
                io.BytesIO(recording_data),
                media_type=content_type or "audio/ogg",
                headers={
                    **cors_headers,
                    "Content-Length": str(size),
                    "Accept-Ranges": "bytes",
                }
            )
        
        # URL fallback...
        raise HTTPException(status_code=404, detail="Recording not found")
        
    except Exception as e:
        logging.error(f"❌ Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    

@router.get("/calls/{call_id}/transcript")
async def get_call_transcript(call_id: str, user=Depends(get_current_user)):
    """Get transcript for a specific call"""
    try:
        conn = db.get_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT transcript
                    FROM call_history
                    WHERE call_id = %s AND user_id = %s
                """, (call_id, user["id"]))
                row = cursor.fetchone()
        finally:
            db.release_connection(conn)
        
        if not row or not row[0]:
            raise HTTPException(status_code=404, detail="Transcript not found")
        
        return JSONResponse({"transcript": row[0]})
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error fetching transcript: {e}")
        raise HTTPException(status_code=500, detail=str(e))