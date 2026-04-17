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
from src.services.google_calendar_service import GoogleCalendarService

from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi import HTTPException, Response, UploadFile, File
from rich import print
from src.api.base_models import (
    UserLogin,
    UserRegister,
    UserOut,
    LoginResponse,
    UpdateUserProfileRequest,
    Assistant_Payload,
    PromptCustomizationUpdate,
    ContactsListResponse,
    ContactUploadResponse,
    ContactUploadResponse,
    ContactUploadStats,
    BulkCallResponse,
    PromptResponse,
    UpdatePromptRequest,
    CreatePromptRequest,
    SingleCallPayload,
    BulkCallPayload,
    AddVoiceRequest, 
    VoiceResponse,
    ForgotPasswordRequest,
    ResetPasswordRequest 

)
from src.utils.db import PGDB 
from src.utils.mail_management import Send_Mail
from src.utils.jwt_utils import create_access_token
from src.utils.utils import get_current_user,add_call_event, generate_presigned_url,fetch_and_store_transcript,fetch_and_store_recording, calculate_duration, check_if_answered
#
# LiveKit removed (Retell is calling provider)
#
from src.models.System_Prompt import PromptBuilder
import csv

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from datetime import datetime
from src.services.google_calendar_service import GoogleCalendarService
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
import json
from twilio.rest import Client
from src.utils.retell_utils import (
    verify_retell_signature,
    extract_retell_event_and_call,
    ms_epoch_to_datetime,
    build_transcript_snapshot,
    retell_get_agent,
    retell_list_voices,
    retell_update_agent,
    retell_publish_agent,
    retell_get_conversation_flow,
    retell_update_conversation_flow,
    retell_create_phone_call,
)

# Google OAuth Configuration
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/google/callback")
GOOGLE_SCOPES = ['https://www.googleapis.com/auth/calendar']

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID")
load_dotenv()

router = APIRouter()
mail_obj = Send_Mail()
db = PGDB()
load_dotenv(override=True)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
AWS_BUCKET_NAME = os.getenv("AWS_S3_BUCKET_NAME")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# error response 
def error_response(message, status_code=400):
    return JSONResponse(
        status_code=status_code,
        content={"error": message}
    )

def send_appointment_confirmation_sms(
    phone_number: str,
    appointment_date: str,
    start_time: str,
    title: str,
    attendee_name: str
):
    """
    Send SMS confirmation through Twilio Messaging Service (with A2P 10DLC campaign)
    """
    try:
        if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_MESSAGING_SERVICE_SID]):
            logging.warning("⚠️ Twilio credentials not configured, skipping SMS")
            return False
        
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        
        # Format date and time
        from datetime import datetime
        date_obj = datetime.strptime(appointment_date, "%Y-%m-%d")
        formatted_date = date_obj.strftime("%B %d, %Y")
        
        time_obj = datetime.strptime(start_time, "%H:%M")
        formatted_time = time_obj.strftime("%I:%M %p")
        
        # Message body
        message_body = f"""✅ Appointment Confirmed!

Date: {formatted_date}
Time: {formatted_time}
Service: {title}
Location: {attendee_name}

Thank you for booking! We look forward to seeing you.

Reply CANCEL to reschedule."""
        
        # ⭐ Send through Messaging Service (automatically uses campaign)
        message = client.messages.create(
            body=message_body,
            messaging_service_sid=TWILIO_MESSAGING_SERVICE_SID,
            to=phone_number
        )
        
        logging.info(f"✅ SMS sent via campaign to {phone_number} (SID: {message.sid})")
        logging.info(f"   Campaign: Low Volume Mixed (CTYJ4B9)")
        return True
        
    except Exception as e:
        logging.error(f"❌ Failed to send SMS: {e}")
        traceback.print_exc()
        return False


@router.post("/retell-webhook")
async def retell_webhook(request: Request):
    """
    Retell account webhook: persist call status/transcript/recording into call_history.
    Outbound project: no inbound routing webhook required.
    """
    raw_body = (await request.body()).decode("utf-8")
    sig = request.headers.get("X-Retell-Signature")
    if not verify_retell_signature(raw_body, sig):
        raise HTTPException(status_code=401, detail="Invalid Retell signature")
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event, call = extract_retell_event_and_call(payload)
    call_id = (call.get("call_id") or "").strip()
    if not call_id:
        return Response(status_code=204)

    # Best-effort: try to resolve user_id from metadata/dynamic vars if present.
    user_id = None
    try:
        meta = call.get("metadata") or {}
        if isinstance(meta, dict) and meta.get("user_id") is not None:
            user_id = int(meta.get("user_id"))
    except Exception:
        user_id = None
    try:
        dyn = call.get("collected_dynamic_variables") or {}
        if user_id is None and isinstance(dyn, dict) and dyn.get("user_id") is not None:
            user_id = int(dyn.get("user_id"))
    except Exception:
        pass

    started_at = ms_epoch_to_datetime(call.get("start_timestamp"))
    ended_at = ms_epoch_to_datetime(call.get("end_timestamp"))
    duration = None
    try:
        if call.get("duration_ms") is not None:
            duration = float(call.get("duration_ms")) / 1000.0
    except Exception:
        duration = None
    recording_url = call.get("recording_url") or call.get("recording_multi_channel_url")
    transcript = build_transcript_snapshot(call)

    # Ensure row exists if we have a user_id, otherwise only append event log if row already exists.
    exists = bool(db.execute("SELECT 1 FROM call_history WHERE call_id=%s LIMIT 1", (call_id,), fetchone=True))
    if not exists and user_id is not None:
        try:
            db.insert_call_history(
                user_id=user_id,
                call_id=call_id,
                status="connected",
                to_number=call.get("to_number"),
                from_number=call.get("from_number"),
                voice_name="retell",
                voice_id=None,
                contact_first_name=None,
                contact_email=None,
                category=None,
                call_outcome_status=None,
            )
        except Exception:
            pass

    updates: dict[str, Any] = {
        "status": (event or call.get("call_status") or call.get("status") or "connected"),
        "from_number": call.get("from_number"),
        "to_number": call.get("to_number"),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration": duration,
        "recording_url": recording_url,
    }
    if transcript:
        updates["transcript"] = transcript
    try:
        # Remove None values to avoid overwriting
        safe = {k: v for k, v in updates.items() if v is not None}
        if safe:
            db.update_call_history(call_id, safe)
    except Exception as e:
        logging.warning("retell-webhook update_call_history failed: %s", e)
    try:
        add_call_event(call_id, f"retell_{event or 'event'}", payload)
    except Exception:
        pass
    return Response(status_code=204)


@router.get("/retell/flow/editor")
async def retell_flow_editor(user=Depends(get_current_user)):
    flow_id = (os.getenv("RETELL_CONVERSATION_FLOW_ID") or "").strip()
    if not flow_id:
        raise HTTPException(status_code=500, detail="RETELL_CONVERSATION_FLOW_ID is not configured")
    flow = retell_get_conversation_flow(flow_id)
    intro_id = flow.get("start_node_id")
    intro_text = None
    nodes = flow.get("nodes") or []
    if intro_id and isinstance(nodes, list):
        for n in nodes:
            if isinstance(n, dict) and str(n.get("id")) == str(intro_id):
                instr = n.get("instruction") or {}
                if isinstance(instr, dict):
                    intro_text = instr.get("text")
                break
    return JSONResponse(
        content=jsonable_encoder(
            {
                "conversation_flow_id": flow.get("conversation_flow_id") or flow_id,
                "version": flow.get("version"),
                "global_prompt": flow.get("global_prompt"),
                "intro_node_id": intro_id,
                "intro_text": intro_text,
            }
        )
    )


@router.put("/retell/flow/prompt-and-intro")
async def retell_flow_prompt_and_intro(body: dict, user=Depends(get_current_user)):
    flow_id = (os.getenv("RETELL_CONVERSATION_FLOW_ID") or "").strip()
    if not flow_id:
        raise HTTPException(status_code=500, detail="RETELL_CONVERSATION_FLOW_ID is not configured")
    global_prompt = body.get("global_prompt")
    intro_node_id = (body.get("intro_node_id") or "").strip() or None
    intro_text = body.get("intro_text")

    updates: dict[str, Any] = {}
    if global_prompt is not None:
        updates["global_prompt"] = str(global_prompt)

    if intro_text is not None:
        current = retell_get_conversation_flow(flow_id)
        nodes = current.get("nodes") or []
        intro_id = intro_node_id or current.get("start_node_id")
        if not intro_id:
            raise HTTPException(status_code=400, detail="No intro node id available")
        new_nodes: list[dict] = []
        found = False
        for n in nodes:
            if not isinstance(n, dict):
                continue
            if str(n.get("id")) == str(intro_id):
                nn = dict(n)
                instr = nn.get("instruction") if isinstance(nn.get("instruction"), dict) else {}
                instr2 = dict(instr)
                instr2["type"] = instr2.get("type") or "prompt"
                instr2["text"] = str(intro_text)
                nn["instruction"] = instr2
                new_nodes.append(nn)
                found = True
            else:
                new_nodes.append(n)
        if not found:
            raise HTTPException(status_code=404, detail=f"Intro node '{intro_id}' not found")
        updates["nodes"] = new_nodes

    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")
    retell_update_conversation_flow(flow_id, updates)
    return JSONResponse(content={"ok": True})


@router.get("/retell/voices")
async def retell_voices(user=Depends(get_current_user)):
    agent_id = (os.getenv("RETELL_AGENT_ID") or "").strip()
    if not agent_id:
        raise HTTPException(status_code=500, detail="RETELL_AGENT_ID is not configured")
    agent = retell_get_agent(agent_id)
    voices = retell_list_voices()

    def _norm(x: Any) -> str:
        return str(x or "").strip().lower()

    def _is_11labs(v: dict) -> bool:
        vid = _norm(v.get("voice_id"))
        prov = _norm(v.get("provider"))
        return vid.startswith("11labs-") or prov in ("11labs", "elevenlabs")

    allowed_lang = {"en", "eng", "english", "es", "spa", "spanish"}
    out: list[dict] = []
    for v in voices:
        if not isinstance(v, dict):
            continue
        if not _is_11labs(v):
            continue
        if _norm(v.get("gender")) != "female":
            continue
        age = _norm(v.get("age")).replace("_", " ").replace("-", " ")
        if "middle" not in age:
            continue
        lang = _norm(v.get("language") or v.get("lang"))
        if lang and lang not in allowed_lang:
            continue
        out.append(
            {
                "voice_id": v.get("voice_id"),
                "voice_name": v.get("voice_name"),
                "provider": v.get("provider"),
                "gender": v.get("gender"),
                "age": v.get("age"),
                "accent": v.get("accent"),
                "language": v.get("language") or v.get("lang"),
                "preview_audio_url": v.get("preview_audio_url"),
            }
        )

    return JSONResponse(content=jsonable_encoder({"current_voice_id": agent.get("voice_id"), "voices": out}))


@router.put("/retell/agent/voice")
async def retell_set_voice(body: dict, user=Depends(get_current_user)):
    agent_id = (os.getenv("RETELL_AGENT_ID") or "").strip()
    if not agent_id:
        raise HTTPException(status_code=500, detail="RETELL_AGENT_ID is not configured")
    voice_id = (body.get("voice_id") or "").strip()
    if not voice_id:
        raise HTTPException(status_code=400, detail="voice_id must be non-empty")
    retell_update_agent(agent_id, {"voice_id": voice_id})
    retell_publish_agent(agent_id)
    return JSONResponse(content={"ok": True, "voice_id": voice_id})
    
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
    "sam elliott":"1Le15oXwaOV6DjrgvGiL",
    "peck":"KP0g0tgE6czKXsf2vmF6",
    "king":"1WVD88RnPY0xX4bYTFi4",
    "barry white":"sydt9eVyT7wySiR0Mcpo",
    "smokey burt":"M7z5dT9mmYi8BD8PhjLd",
    "dark blues singer":"FMUdRwz26PiAESHyfVut",
    "wyatt":"YXpFCvM1S3JbWEJhoskW",
    "southern mike":"DwEFbvGTcJhAk9eY9m0f",
    "serafina":"4tRn1lSkEn13EVTuqb0g",
    "paul":"6677dBjGbnngIll0IDYQ"
}

@router.post("/assistant-bulk-call")
async def assistant_bulk_call_retell(
    payload: BulkCallPayload,
    user=Depends(get_current_user),
):
    """
    Outbound call initiation via Retell (v2/create-phone-call).
    Creates one Retell call per phone number and stores the returned call_id in DB.
    """
    agent_id = (os.getenv("RETELL_AGENT_ID") or "").strip()
    from_number = (
        os.getenv("RETELL_FROM_NUMBER")
        or os.getenv("RETELL_OUTBOUND_FROM_NUMBER")
        or os.getenv("RETELL_OUTBOUND_NUMBER")
        or ""
    ).strip()
    if not agent_id:
        raise HTTPException(status_code=500, detail="RETELL_AGENT_ID is not configured")
    if not from_number:
        raise HTTPException(status_code=500, detail="RETELL_FROM_NUMBER is not configured")

    initiated_calls: list[dict] = []
    failed_calls: list[dict] = []

    # Fetch agent once for current voice_id (optional for DB/UI)
    agent = None
    try:
        agent = retell_get_agent(agent_id)
    except Exception:
        agent = None
    current_voice_id = (agent or {}).get("voice_id") if isinstance(agent, dict) else None

    for idx, to_number in enumerate(payload.phone_numbers or []):
        try:
            phone = (to_number or "").strip()
            if not phone:
                raise ValueError("empty phone number")

            contact_first_name = None
            if getattr(payload, "first_names", None) and len(payload.first_names) == len(payload.phone_numbers):
                contact_first_name = payload.first_names[idx]
            else:
                contact_first_name = getattr(payload, "first_name", None)

            meta = {
                "user_id": str(user["id"]),
                "category": str(getattr(payload, "category", "") or ""),
                "contact_first_name": str(contact_first_name or ""),
                "contact_email": str(getattr(payload, "email", "") or ""),
            }
            dyn = {
                "user_id": str(user["id"]),
                "contact_first_name": str(contact_first_name or ""),
                "contact_email": str(getattr(payload, "email", "") or ""),
                "category": str(getattr(payload, "category", "") or ""),
            }

            resp = retell_create_phone_call(
                from_number=from_number,
                to_number=phone,
                override_agent_id=agent_id,
                metadata=meta,
                dynamic_variables=dyn,
            )
            call_id = (resp.get("call_id") or "").strip()
            if not call_id:
                raise RuntimeError("Retell did not return call_id")

            # Store initial DB row (status updated later via /retell-webhook)
            db.insert_call_history(
                user_id=user["id"],
                call_id=call_id,
                status="initiated",
                voice_id=current_voice_id,
                voice_name=None,
                to_number=phone,
                contact_first_name=contact_first_name,
                contact_email=getattr(payload, "email", None),
                category=getattr(payload, "category", None),
                call_outcome_status=None,
            )
            try:
                add_call_event(call_id, "retell_call_initiated", {"to_number": phone})
            except Exception:
                pass

            initiated_calls.append(
                {
                    "call_id": call_id,
                    "to_number": phone,
                }
            )
        except Exception as e:
            failed_calls.append({"to_number": to_number, "error": str(e)})

    return JSONResponse(
        content=jsonable_encoder(
            {
                "success": True,
                "total": len(payload.phone_numbers or []),
                "initiated": len(initiated_calls),
                "failed": len(failed_calls),
                "calls": initiated_calls,
                "errors": failed_calls,
            }
        )
    )




# @router.post("/assistant-initiate-call")
# async def make_call_with_livekit(payload: Assistant_Payload, user=Depends(get_current_user)):
#     try:
#         room_name = f"call-{user['id']}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
#         #  Get voice_id from payload.voice name
#         voice_name = getattr(payload, "voice", "david").lower()  # Default to 'david'
#         voice_id = voices.get(voice_name)
        
#         if not voice_id:
#             logging.warning(f" Unknown voice '{voice_name}', using default 'david'")
#             voice_id = voices["david"]
#             voice_name = "david"
        
#         #  Get language from payload (default to 'en')
#         language = getattr(payload, "language", "en").lower()
#         if language not in ["en", "es"]:
#             logging.warning(f" Unknown language '{language}', defaulting to 'en'")
#             language = "en"
        
#         logging.info(f" Using voice: {voice_name} (ID: {voice_id}), Language: {language}")
        
#         #  STEP 1: Get user's custom prompt from DB
#         user_prompt_data = db.get_user_prompt(user["id"])
        
#         if not user_prompt_data:
#             return error_response("User prompt not found", status_code=404)
        
#         base_prompt = user_prompt_data["system_prompt"]
        
#         #  STEP 2: Build complete system prompt
#         prompt_builder = SystemPromptBuilder(
#             base_prompt=base_prompt,
#             caller_name=payload.caller_name,
#             caller_email=payload.caller_email,
#             call_context=payload.context,
#             language=language  

#         )
        
#         complete_system_prompt = prompt_builder.generate_complete_prompt()
        
#         logging.info(f" Built system prompt ({len(complete_system_prompt)} chars)")
        
#         #  STEP 3: Prepare metadata with complete prompt + voice + language
#         metadata = {
#             "phone_number": payload.outbound_number,
#             "call_context": payload.context,
#             "user_id": user["id"],
#             "caller_name": payload.caller_name,
#             # "caller_email": payload.caller_email,
#             "caller_email": user["email"],#payload.caller_email,
#             "system_prompt": complete_system_prompt,
#             "agent_name": "PAUL",
#             "voice_id": voice_id,        
#             "voice_name": voice_name,    
#             "language": language         
#         }
#         print("\n\n")
#         print(metadata)
#         print("\n\n")

#         #  STEP 4: Create DB record
#         db.insert_call_history(
#             user_id=user["id"],
#             call_id=room_name,
#             status="initiated",
#             to_number=payload.outbound_number,
#             voice_name=voice_name,  
#         )
#         logging.info(f" Created call record: {room_name}")

#         add_call_event(room_name, "call_initiated", {"user_id": user["id"]})

#         async with api.LiveKitAPI(
#             url=os.getenv("LIVEKIT_URL", "").replace("wss://", "https://"),
#             api_key=os.getenv("LIVEKIT_API_KEY"),
#             api_secret=os.getenv("LIVEKIT_API_SECRET"),
#         ) as lkapi:
#             dispatch = await lkapi.agent_dispatch.create_dispatch(
#                 api.CreateAgentDispatchRequest(
#                     agent_name="outbound-caller",
#                     room=room_name,
#                     metadata=json.dumps(metadata),
#                 )
#             )

#         logging.info(f" Agent dispatched: {dispatch.id}")

#         return JSONResponse({
#             "success": True,
#             "call_id": room_name,
#             "dispatch_id": dispatch.id,
#             "voice": voice_name,
#             "language": language,
#             "message": "Call initiated successfully"
#         })
        
#     except Exception as e:
#         logging.error(f"Error initiating LiveKit call: {e}")
#         traceback.print_exc()
        
#         if 'room_name' in locals():
#             try:
#                 db.update_call_history(
#                     call_id=room_name,
#                     updates={"status": "failed"}
#                 )
#             except:
#                 pass
        
#         raise HTTPException(status_code=500, detail=f"Failed to initiate call: {str(e)}"


    




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

            # Explicitly surface phone number and outcome status for each entry
            call_data["phone_number"] = call.get("to_number")
            call_data["call_outcome_status"] = call.get("call_outcome_status")
            call_data["contact_call_status"] = call.get("contact_call_status")
            
            
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
            
            call_data["has_recording"] = bool(call.get("recording_blob"))
            if call.get("recording_blob"):
                presigned_url = generate_presigned_url(call["recording_blob"], expiration=3600)
                call_data["recording_presigned_url"] = presigned_url
            else:
                call_data["recording_presigned_url"] = None
            
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
    try:
        data = await request.json()
        
        user_id = data.get("user_id")
        appointment_date = data.get("appointment_date")
        start_time = data.get("start_time")
        end_time = data.get("end_time")
        title = data.get("title", "Appointment")
        description = data.get("description", "")
        attendee_name = data.get("attendee_name", "")
        organizer_email = data.get("organizer_email")
        organizer_name = data.get("organizer_name", "")
        notes = data.get("notes", "")
        phone_number = data.get("phone_number")
        
        logging.info(f"Booking appointment for user {user_id}: {appointment_date} {start_time}-{end_time}")
        
        if not all([user_id, appointment_date, start_time, end_time]):
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Missing required fields"}
            )
        
        google_event_id = None
        google_success = False
        
        try:
            credentials = db.get_google_credentials(user_id)
            
            if credentials:
                logging.info("User has Google Calendar connected")
                
                from src.services.google_calendar_service import GoogleCalendarService
                gcal = GoogleCalendarService(credentials)
                
                date_obj = datetime.fromisoformat(appointment_date)
                start_hour, start_min = map(int, start_time.split(':'))
                end_hour, end_min = map(int, end_time.split(':'))
                
                start_datetime = date_obj.replace(hour=start_hour, minute=start_min, tzinfo=timezone.utc)
                end_datetime = date_obj.replace(hour=end_hour, minute=end_min, tzinfo=timezone.utc)
                
                attendees = [organizer_email] if organizer_email else []
                
                full_description = description
                if notes:
                    full_description += f"\n\nNotes: {notes}"
                
                event = gcal.create_event(
                    summary=title,
                    start_datetime=start_datetime,
                    end_datetime=end_datetime,
                    description=full_description,
                    location=attendee_name,
                    attendees=attendees
                )
                
                google_event_id = event["id"]
                google_success = True
                
                logging.info(f"Google Calendar event created: {google_event_id}")
                
                updated_creds = gcal.get_updated_credentials()
                if updated_creds['access_token'] != credentials['access_token']:
                    db.save_google_credentials(user_id, **updated_creds)
            else:
                logging.warning("User does not have Google Calendar connected")
        
        except Exception as google_error:
            logging.error(f"Google Calendar error: {google_error}")
            traceback.print_exc()
        
        try:
            appointment_id = db.create_appointment(
                user_id=user_id,
                appointment_date=appointment_date,
                start_time=start_time,
                end_time=end_time,
                attendee_name=attendee_name,
                attendee_email=organizer_email or "",
                title=title,
                description=description + (f"\n\nNotes: {notes}" if notes else "")
            )
            
            logging.info(f"Appointment saved to database: ID {appointment_id}")
            
            if phone_number:
                try:
                    sms_sent = send_appointment_confirmation_sms(
                        phone_number=phone_number,
                        appointment_date=appointment_date,
                        start_time=start_time,
                        title=title,
                        attendee_name=attendee_name
                    )
                    if sms_sent:
                        logging.info(f"SMS confirmation sent to {phone_number}")
                except Exception as sms_error:
                    logging.error(f"SMS failed: {sms_error}")
            
            response = {
                "success": True,
                "message": "Appointment booked successfully",
                "appointment_id": appointment_id,
            }
            
            if google_success:
                response["google_event_id"] = google_event_id
                response["google_calendar"] = True
            else:
                response["google_calendar"] = False
                response["note"] = "Saved locally"
            
            return JSONResponse(content=response)
            
        except Exception as db_error:
            logging.error(f"Database error: {db_error}")
            traceback.print_exc()
            
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": f"Failed to save appointment: {str(db_error)}"}
            )
        
    except Exception as e:
        logging.error(f"Error in book_appointment: {e}")
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": f"Failed to book appointment: {str(e)}"}
        )

@router.post("/agent/save-call-data")
async def save_call_data(request: Request):
    try:
        data = await request.json()
        
        call_id = data.get("call_id")
        transcript_blob = data.get("transcript_blob")  # S3 key
        recording_blob = data.get("recording_blob")    # S3 key
        
        updates = {
            "transcript_blob": transcript_blob,
            "recording_blob": recording_blob
        }
        
        db.update_call_history(call_id, updates)
        
        # Download from S3 (delayed)
        if transcript_blob:
            async def delayed_transcript():
                await asyncio.sleep(5)
                logging.info(f" Downloading transcript from S3")
                await fetch_and_store_transcript(call_id, None, transcript_blob)
            asyncio.create_task(delayed_transcript())
        
        
        return JSONResponse({"success": True})
        
    except Exception as e:
        logging.error(f" save_call_data error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    




    











@router.post("/prompts")
async def create_prompt(
    request: Request,
    user=Depends(get_current_user)
):
    """
    Create a new named prompt.
    
    Body:
    {
        "prompt_name": "Sales Call Script",
        "system_prompt": "You are a sales assistant..."
    }
    """
    try:
        data = await request.json()
        
        prompt_name = data.get("prompt_name", "").strip()
        system_prompt = data.get("system_prompt", "").strip()
        
        if not prompt_name:
            return error_response("prompt_name is required", status_code=400)
        
        if not system_prompt:
            return error_response("system_prompt is required", status_code=400)
        
        if len(prompt_name) > 255:
            return error_response("prompt_name too long (max 255 chars)", status_code=400)
        
        result = db.create_prompt(user["id"], prompt_name, system_prompt)
        
        return JSONResponse(content=jsonable_encoder({
            "success": True,
            "message": "Prompt created successfully",
            "prompt": result
        }), status_code=201)
        
    except ValueError as ve:
        return error_response(str(ve), status_code=400)
    except Exception as e:
        logging.error(f"Error creating prompt: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/prompts")
async def get_all_prompts(user=Depends(get_current_user)):
    """
    Get all prompts for the current user with names and content.
    
    Returns:
    {
        "success": true,
        "prompts": [
            {
                "id": 1,
                "prompt_name": "Default Prompt",
                "system_prompt": "...",
                "is_default": true,
                "created_at": "...",
                "updated_at": "..."
            },
            ...
        ]
    }
    """
    try:
        prompts = db.get_all_user_prompts(user["id"])
        
        return JSONResponse(content=jsonable_encoder({
            "success": True,
            "count": len(prompts),
            "prompts": prompts
        }))
        
    except Exception as e:
        logging.error(f"Error fetching prompts: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/prompts/{prompt_id}")
async def get_prompt_by_id(
    prompt_id: int,
    user=Depends(get_current_user)
):
    """
    Get a specific prompt by ID.
    """
    try:
        prompt = db.get_prompt_by_id(user["id"], prompt_id)
        
        if not prompt:
            return error_response("Prompt not found", status_code=404)
        
        return JSONResponse(content=jsonable_encoder({
            "success": True,
            "prompt": prompt
        }))
        
    except Exception as e:
        logging.error(f"Error fetching prompt: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/prompts/{prompt_id}")
async def update_prompt(
    prompt_id: int,
    request: Request,
    user=Depends(get_current_user)
):
    """
    Update an existing prompt.
    
    Body (both optional, but at least one required):
    {
        "prompt_name": "Updated Name",
        "system_prompt": "Updated prompt..."
    }
    """
    try:
        data = await request.json()
        
        prompt_name = data.get("prompt_name", "").strip() if "prompt_name" in data else None
        system_prompt = data.get("system_prompt", "").strip() if "system_prompt" in data else None
        
        if prompt_name is not None and not prompt_name:
            return error_response("prompt_name cannot be empty", status_code=400)
        
        if system_prompt is not None and not system_prompt:
            return error_response("system_prompt cannot be empty", status_code=400)
        
        if prompt_name is None and system_prompt is None:
            return error_response("At least one field must be provided", status_code=400)
        
        result = db.update_prompt(user["id"], prompt_id, prompt_name, system_prompt)
        
        return JSONResponse(content=jsonable_encoder({
            "success": True,
            "message": "Prompt updated successfully",
            "prompt": result
        }))
        
    except ValueError as ve:
        return error_response(str(ve), status_code=400)
    except Exception as e:
        logging.error(f"Error updating prompt: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/prompts/{prompt_id}")
async def delete_prompt(
    prompt_id: int,
    user=Depends(get_current_user)
):
    """
    Delete a prompt (cannot delete default).
    """
    try:
        db.delete_prompt(user["id"], prompt_id)
        
        return JSONResponse(content={
            "success": True,
            "message": "Prompt deleted successfully"
        })
        
    except ValueError as ve:
        return error_response(str(ve), status_code=400)
    except Exception as e:
        logging.error(f"Error deleting prompt: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/prompts/{prompt_id}/set-default")
async def set_default_prompt(
    prompt_id: int,
    user=Depends(get_current_user)
):
    """
    Set a prompt as the default.
    """
    try:
        result = db.set_default_prompt(user["id"], prompt_id)
        
        return JSONResponse(content=jsonable_encoder({
            "success": True,
            "message": "Default prompt updated",
            "prompt": result
        }))
        
    except ValueError as ve:
        return error_response(str(ve), status_code=400)
    except Exception as e:
        logging.error(f"Error setting default: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    

@router.get("/calls/{call_id}/recording")
async def get_call_recording_url(
    call_id: str,
    user=Depends(get_current_user)
):
    """
    Get presigned URL for call recording (valid for 1 hour).
    Recording is stored in S3, not in DB.
    """
    try:
        
        # Get recording blob path from DB
        with db.conn() as (conn, cursor):
            cursor.execute("""
                SELECT recording_blob
                FROM call_history
                WHERE call_id = %s AND user_id = %s
            """, (call_id, user["id"]))
            row = cursor.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="Call not found")
        
        recording_blob = row["recording_blob"] if isinstance(row, dict) else row[0]
        
        if not recording_blob:
            raise HTTPException(status_code=404, detail="No recording available")
        
        # Generate presigned URL (valid for 1 hour)
        presigned_url = generate_presigned_url(recording_blob, expiration=3600)
        
        if not presigned_url:
            raise HTTPException(status_code=500, detail="Failed to generate download URL")
        
        return JSONResponse({
            "success": True,
            "recording_url": presigned_url,
            "expires_in": 3600,
            "call_id": call_id
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error getting recording URL: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    

    
@router.get("/calls/{call_id}/transcript")
async def get_call_transcript(call_id: str, user=Depends(get_current_user)):
    """Get transcript for a specific call"""
    try:
        #  Use the context manager pattern
        with db.conn() as (conn, cursor):
            cursor.execute("""
                SELECT transcript
                FROM call_history
                WHERE call_id = %s AND user_id = %s
            """, (call_id, user["id"]))
            row = cursor.fetchone()
        
        if not row or not row["transcript"]:
            raise HTTPException(status_code=404, detail="Transcript not found")
        
        return JSONResponse({"transcript": row["transcript"]})
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error fetching transcript: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    


@router.post("/contacts/upload", response_model=ContactUploadResponse)
async def upload_contacts_csv(
    file: UploadFile = File(...),
    user=Depends(get_current_user)
):
    """
    Upload CSV file with contacts (name, phone, email)
    Fast bulk insert - no row-by-row validation
    """
    try:
        # Validate file type
        if not file.filename.endswith('.csv'):
            raise HTTPException(status_code=400, detail="File must be a CSV")
        
        # Read file content
        content = await file.read()
        decoded = content.decode('utf-8-sig')
        csv_file = io.StringIO(decoded)
        
        # Parse CSV quickly
        csv_reader = csv.DictReader(csv_file)
        
        # Build contact list (minimal validation)
        contacts = []
        for row in csv_reader:
            # Get phone (flexible column names)
            phone = (
                row.get('Phone number') or 
                row.get('phone_number') or 
                row.get('phone') or 
                row.get('Phone') or ""
            ).strip()
            
            # Quick clean
            phone = ''.join(c for c in phone if c.isdigit())
            
            # Skip if no phone or invalid length
            if not phone or len(phone) < 10:
                continue
            
            contacts.append({
                "first_name": (row.get('First name') or row.get('first_name') or "").strip()[:100],
                "last_name": (row.get('Last name') or row.get('last_name') or "").strip()[:100],
                "phone_number": phone,
                "email": (row.get('Email address') or row.get('email') or "").strip()[:255] or None
            })
        
        if not contacts:
            raise HTTPException(status_code=400, detail="No valid contacts found")
        
        # Use FAST bulk insert
        stats = db.save_contacts_bulk(user["id"], contacts)
        
        return {
            "success": True,
            "message": f"Successfully processed {stats['inserted']} contacts",
            "stats": {
                "total_rows": len(contacts),
                "inserted": stats['inserted'],
                "duplicates": stats['duplicates'],
                "skipped": 0,
                "errors": 0
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error uploading contacts: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/contacts")
async def get_contacts_simple_list(user=Depends(get_current_user)):
    """
    Get all contacts (name + phone only) - fast, no pagination
    Perfect for dropdown lists or quick display
    """
    try:
        contacts = db.get_contacts_simple(user["id"])
        
        return JSONResponse({
            "success": True,
            "count": len(contacts),
            "contacts": contacts
        })
        
    except Exception as e:
        logging.error(f"Error fetching simple contacts: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch contacts: {str(e)}"
        )



#
# LiveKit webhook/status endpoints removed (Retell is the calling provider).
#


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
        
        updates = {"status": status}
        now = datetime.now(timezone.utc)
        
        # Set started_at on dialing or connected
        if status in {"dialing", "connected"}:
            #  FIXED: Use context manager
            with db.conn() as (conn, cursor):
                cursor.execute(
                    "SELECT started_at FROM call_history WHERE call_id = %s",
                    (call_id,)
                )
                row = cursor.fetchone()
                if row:
                    started = row.get("started_at") if isinstance(row, dict) else row[0]
                    if not started:
                        updates["started_at"] = now
        
        # Handle unanswered
        if status == "unanswered":
            updates["ended_at"] = now
            updates["duration"] = 0
        
        db.update_call_history(call_id, updates)
        
        return JSONResponse({"success": True})
        
    except Exception as e:
        logging.error(f"report-event error: {e}")
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/agent/update-call-outcome")
async def update_call_outcome(request: Request):
    """Allow the agent to mark call outcome (booked, call_again, do_not_call) and sync contacts."""
    try:
        data = await request.json()
        call_id = data.get("call_id")
        outcome = (data.get("status") or "").strip().lower()

        allowed = {"booked", "call_again", "do_not_call"}
        if not call_id or outcome not in allowed:
            return JSONResponse({"error": "Invalid call_id or status"}, status_code=400)

        # Fetch user + phone for contact sync
        with db.conn() as (conn, cursor):
            cursor.execute(
                """
                SELECT user_id, to_number
                FROM call_history
                WHERE call_id = %s
                """,
                (call_id,)
            )
            row = cursor.fetchone()

        if not row:
            return JSONResponse({"error": "Call not found"}, status_code=404)

        user_id = row.get("user_id") if isinstance(row, dict) else row[0]
        to_number = row.get("to_number") if isinstance(row, dict) else row[1]

        db.update_call_history(call_id, {"call_outcome_status": outcome})

        if user_id and to_number:
            try:
                db.update_contact_status(user_id, to_number, outcome)
            except Exception:
                logging.warning("Unable to update contact status; continuing")

        add_call_event(call_id, "call_outcome", {"status": outcome})

        return JSONResponse({"success": True, "status": outcome})

    except Exception as e:
        logging.error(f"update_call_outcome error: {e}")
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/analytics/call-outcomes")
async def get_call_outcomes_summary(user=Depends(get_current_user)):
    """Return counts of call outcome statuses for the signed-in user."""
    try:
        summary = db.get_call_outcome_summary(user["id"])
        return JSONResponse({"success": True, "summary": summary})
    except Exception as e:
        logging.error(f"analytics call outcomes error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to fetch call outcome analytics")
    




@router.post("/voices", response_model=VoiceResponse)
async def add_new_voice(
    request: Request,
    user=Depends(get_current_user)
):
    """
    Add a new voice to the global library.
    
    Body:
    {
        "voice_name": "Morgan Freeman",
        "voice_id": "abc123xyz"
    }
    """
    try:
        data = await request.json()
        
        voice_name = data.get("voice_name", "").strip()
        voice_id = data.get("voice_id", "").strip()
        
        if not voice_name:
            return error_response("voice_name is required", status_code=400)
        
        if not voice_id:
            return error_response("voice_id is required", status_code=400)
        
        if len(voice_name) > 255:
            return error_response("voice_name too long (max 255 chars)", status_code=400)
        
        result = db.add_voice(voice_name, voice_id)
        
        return JSONResponse(content=jsonable_encoder({
            "success": True,
            "message": "Voice added successfully",
            "voice": result
        }), status_code=201)
        
    except ValueError as ve:
        return error_response(str(ve), status_code=400)
    except Exception as e:
        logging.error(f"Error adding voice: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/voices")
async def get_all_voices_list(user=Depends(get_current_user)):
    """
    Get all available voices from the global library.
    
    Returns:
    {
        "success": true,
        "count": 15,
        "voices": [
            {
                "voice_name": "Sam Elliott",
                "voice_id": "1Le15oXwaOV6DjrgvGiL"
            },
            ...
        ]
    }
    """
    try:
        voices = db.get_all_voices()
        
        return JSONResponse(content=jsonable_encoder({
            "success": True,
            "count": len(voices),
            "voices": voices
        }))
        
    except Exception as e:
        logging.error(f"Error fetching voices: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/voices/{voice_id}")
async def delete_voice_endpoint(
    voice_id: str,
    user=Depends(get_current_user)
):
    """
    Delete a voice from the library by its voice_id.
    """
    try:
        db.delete_voice(voice_id)
        
        return JSONResponse(content={
            "success": True,
            "message": "Voice deleted successfully"
        })
        
    except Exception as e:
        logging.error(f"Error deleting voice: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    






@router.get("/google/auth/status")
async def get_google_auth_status(user=Depends(get_current_user)):
    """
    Check if user has connected Google Calendar
    """
    try:
        credentials = db.get_google_credentials(user["id"])
        
        if not credentials:
            return JSONResponse({
                "connected": False,
                "message": "Google Calendar not connected"
            })
        
        # Check if token is expired
        token_expiry = credentials.get("token_expiry")
        is_expired = False
        
        if token_expiry:
            if isinstance(token_expiry, str):
                token_expiry = datetime.fromisoformat(token_expiry.replace('Z', '+00:00'))
            is_expired = token_expiry < datetime.now(timezone.utc)
        
        return JSONResponse({
            "connected": True,
            "expired": is_expired,
            "message": "Google Calendar connected" if not is_expired else "Token expired, needs refresh"
        })
        
    except Exception as e:
        logging.error(f"Error checking Google auth status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/google/auth/login")
async def google_calendar_login(user=Depends(get_current_user)):
    """
    Initiate Google OAuth flow
    Returns authorization URL for frontend to redirect to
    """
    try:
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [GOOGLE_REDIRECT_URI]
                }
            },
            scopes=GOOGLE_SCOPES
        )
        
        flow.redirect_uri = GOOGLE_REDIRECT_URI
        
        # Generate authorization URL with user_id in state
        authorization_url, state = flow.authorization_url(
            access_type='offline',
            include_granted_scopes='true',
            state=str(user["id"]),  # Pass user_id in state
            prompt='consent'  # Force consent to get refresh token
        )
        
        return JSONResponse({
            "authorization_url": authorization_url,
            "state": state
        })
        
    except Exception as e:
        logging.error(f"Error initiating Google OAuth: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/google/callback")
async def google_calendar_callback(
    code: str = Query(...),
    state: str = Query(...)
):
    """
    Handle Google OAuth callback
    Exchanges authorization code for access token
    """
    try:
        # Extract user_id from state
        user_id = int(state)
        
        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [GOOGLE_REDIRECT_URI]
                }
            },
            scopes=GOOGLE_SCOPES
        )
        
        flow.redirect_uri = GOOGLE_REDIRECT_URI
        
        # Exchange authorization code for tokens
        flow.fetch_token(code=code)
        
        credentials = flow.credentials
        
        # Save to database
        db.save_google_credentials(
            user_id=user_id,
            access_token=credentials.token,
            refresh_token=credentials.refresh_token,
            token_expiry=credentials.expiry,
            scopes=credentials.scopes
        )
        
        logging.info(f"✅ Google Calendar connected for user {user_id}")
        
        # --- THIS IS THE FIX ---
        # We redirect the user to the frontend settings page
        # You can hardcode your domain or use the env variable
        frontend_url = os.getenv("FRONTEND_URL", "https://dialer.joinironfathers.com")
        
        return RedirectResponse(url=f"{frontend_url}/success")
        
    except Exception as e:
        logging.error(f"Error in Google OAuth callback: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
        


@router.post("/google/disconnect")
async def disconnect_google_calendar(user=Depends(get_current_user)):
    """
    Disconnect Google Calendar (delete stored credentials)
    """
    try:
        db.delete_google_credentials(user["id"])
        
        return JSONResponse({
            "success": True,
            "message": "Google Calendar disconnected"
        })
        
    except Exception as e:
        logging.error(f"Error disconnecting Google Calendar: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/google/events")
async def get_google_calendar_events(
    from_date: str = Query(None),
    to_date: str = Query(None),
    user=Depends(get_current_user)
):
    """
    Get events from user's Google Calendar
    """
    try:
        
        
        # Get user's credentials
        credentials = db.get_google_credentials(user["id"])
        
        if not credentials:
            raise HTTPException(
                status_code=401,
                detail="Google Calendar not connected. Please connect first."
            )
        
        gcal = GoogleCalendarService(credentials)
        
        time_min = datetime.fromisoformat(from_date) if from_date else datetime.now(timezone.utc)
        time_max = datetime.fromisoformat(to_date) if to_date else None
        
        events = gcal.list_events(time_min=time_min, time_max=time_max)
        
        updated_creds = gcal.get_updated_credentials()
        if updated_creds['access_token'] != credentials['access_token']:
            db.save_google_credentials(user["id"], **updated_creds)
        
        return JSONResponse({
            "success": True,
            "count": len(events),
            "events": jsonable_encoder(events)
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error fetching Google Calendar events: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    

@router.get("/agent/get-google-credentials/{user_id}")
async def get_user_google_credentials(user_id: int):
    """
    Internal endpoint for agent to get user's Google credentials
    """
    try:
        credentials = db.get_google_credentials(user_id)
        
        if not credentials:
            return JSONResponse(
                status_code=404,
                content={"error": "Google Calendar not connected"}
            )
        
        return JSONResponse({
            "success": True,
            "credentials": {
                "access_token": credentials["access_token"],
                "refresh_token": credentials["refresh_token"],
                "token_expiry": credentials["token_expiry"].isoformat() if credentials["token_expiry"] else None,
                "scopes": credentials["scopes"]
            }
        })
        
    except Exception as e:
        logging.error(f"Error getting credentials: {e}")
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


@router.get("/agent/get-google-appointments/{user_id}")
async def get_google_appointments_for_agent(
    user_id: int,
    date: str = Query(None)
):
    """
    API for agent to get appointments from Google Calendar
    """
    try:
        
        
        # Get credentials
        credentials = db.get_google_credentials(user_id)
        
        if not credentials:
            return JSONResponse({
                "success": False,
                "appointments": [],
                "message": "Google Calendar not connected"
            })
        
        # Initialize service
        gcal = GoogleCalendarService(credentials)
        
        # Parse date or use today
        if date:
            target_date = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
        else:
            target_date = datetime.now(timezone.utc)
        
        # Get events for the specific date
        start_of_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = target_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        events = gcal.list_events(time_min=start_of_day, time_max=end_of_day)
        
        # Format for agent
        appointments = [
            {
                "id": event["id"],
                "date": event["date"],
                "start_time": event["start_time"],
                "end_time": event["end_time"],
                "title": event["summary"],
                "description": event.get("description", ""),
                "location": event.get("location", "")
            }
            for event in events
        ]
        
        updated_creds = gcal.get_updated_credentials()
        if updated_creds['access_token'] != credentials['access_token']:
            db.save_google_credentials(user_id, **updated_creds)
        
        return JSONResponse({
            "success": True,
            "appointments": appointments,
            "count": len(appointments)
        })
        
    except Exception as e:
        logging.error(f"Error getting Google appointments: {e}")
        traceback.print_exc()
        return JSONResponse({
            "success": False,
            "appointments": [],
            "error": str(e)
        })


@router.post("/agent/book-google-appointment")
async def book_google_appointment_for_agent(request: Request):
    """
    API for agent to book appointment in Google Calendar
    """
    try:
        from src.services.google_calendar_service import GoogleCalendarService
        
        data = await request.json()
        
        user_id = data.get("user_id")
        appointment_date = data.get("appointment_date")
        start_time = data.get("start_time")
        end_time = data.get("end_time")
        title = data.get("title", "Appointment")
        description = data.get("description", "")
        location = data.get("location", "")
        attendee_name = data.get("attendee_name", "")
        organizer_email = data.get("organizer_email")
        
        # Validate required fields
        if not all([user_id, appointment_date, start_time, end_time]):
            return error_response("Missing required fields", status_code=400)
        
        # Get user's Google credentials
        credentials = db.get_google_credentials(user_id)
        
        if not credentials:
            return error_response(
                "Google Calendar not connected. Please connect first.",
                status_code=401
            )
        
        gcal = GoogleCalendarService(credentials)
        
        date_obj = datetime.fromisoformat(appointment_date)
        start_hour, start_min = map(int, start_time.split(':'))
        end_hour, end_min = map(int, end_time.split(':'))
        
        start_datetime = date_obj.replace(hour=start_hour, minute=start_min, tzinfo=timezone.utc)
        end_datetime = date_obj.replace(hour=end_hour, minute=end_min, tzinfo=timezone.utc)
        
        is_available = gcal.check_availability(start_datetime, end_datetime)
        
        if not is_available:
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "message": "Time slot already booked in Google Calendar",
                    "conflict": True
                }
            )
        
        # Create event in Google Calendar
        attendees = [organizer_email] if organizer_email else []
        
        event = gcal.create_event(
            summary=title,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            description=description,
            location=location,
            attendees=attendees
        )
        
        # Also save to local database as backup
        db.create_appointment(
            user_id=user_id,
            appointment_date=appointment_date,
            start_time=start_time,
            end_time=end_time,
            attendee_name=attendee_name,
            attendee_email=organizer_email or "",
            title=title,
            description=description
        )
        
        # Update credentials if refreshed
        updated_creds = gcal.get_updated_credentials()
        if updated_creds['access_token'] != credentials['access_token']:
            db.save_google_credentials(user_id, **updated_creds)
        
        logging.info(f" Appointment booked in Google Calendar: {event['id']}")
        
        return JSONResponse({
            "success": True,
            "google_event_id": event["id"],
            "message": "Appointment booked successfully in Google Calendar"
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error booking Google appointment: {e}")
        traceback.print_exc()
        return error_response(
            f"Failed to book appointment: {str(e)}",
            status_code=500
        )
    

@router.post("/forgot-password")
async def forgot_password(request: ForgotPasswordRequest):
    """Send password reset email"""
    try:
        email = request.email.strip().lower()
        
        # Use the context manager pattern
        with db.conn() as (conn, cursor):
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
        
        if not user:
            logging.warning(f"Password reset requested for non-existent email: {email}")
            return JSONResponse({
                "success": True,
                "message": "If that email exists, a reset link has been sent."
            })
        
        from src.utils.jwt_utils import create_password_reset_token
        reset_token = create_password_reset_token(email)
        
        frontend_url = os.getenv("FRONTEND_URL")
        email_sent = await mail_obj.send_password_reset_email(email, reset_token, frontend_url)
        
        return JSONResponse({
            "success": True,
            "message": "If that email exists, a reset link has been sent."
        })
        
    except Exception as e:
        logging.error(f"Error in forgot password: {e}")
        traceback.print_exc()
        return error_response("Failed to process request", 500)

@router.post("/reset-password")
async def reset_password(request: ResetPasswordRequest):
    """Reset password using token"""
    try:
        from src.utils.jwt_utils import verify_password_reset_token
        
        email = verify_password_reset_token(request.token)
        
        if not email:
            return error_response("Invalid or expired reset token", 400)
        
        # Update password
        db.update_user_password(email, request.new_password)
        
        return JSONResponse({
            "success": True,
            "message": "Password updated successfully. You can now login."
        })
        
    except ValueError as e:
        return error_response(str(e), 400)
    except Exception as e:
        logging.error(f"Error resetting password: {e}")
        traceback.print_exc()
        return error_response("Failed to reset password", 500)