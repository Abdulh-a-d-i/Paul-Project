
from __future__ import annotations
import tenacity
import asyncio
import logging
import os
import json
import base64
from typing import Any
from datetime import datetime, timedelta,timezone
import traceback
import httpx
from dotenv import load_dotenv

# Google Cloud imports
from google.cloud import storage
from google.oauth2 import service_account

# LiveKit imports
from livekit import rtc, api
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
    function_tool,
    RunContext,
    get_job_context,
    RoomInputOptions,
    BackgroundAudioPlayer,
    AudioConfig,
    BuiltinAudioClip
)
from livekit.plugins import deepgram, elevenlabs, openai, silero
from livekit.plugins.turn_detector.english import EnglishModel

from livekit.plugins.turn_detector.multilingual import MultilingualModel

load_dotenv(".env")

logger = logging.getLogger("outbound-caller")
logger.setLevel(logging.INFO)

# Environment variables
OUTBOUND_TRUNK_ID = os.getenv("SIP_OUTBOUND_TRUNK_ID")
BACKEND_API_URL = os.getenv("BACKEND_API_URL")
GOOGLE_BUCKET_NAME = os.getenv("GOOGLE_BUCKET_NAME") or os.getenv("GCS_BUCKET_NAME")
GCP_KEY_B64 = os.getenv("GCP_SERVICE_ACCOUNT_KEY_BASE64")
UPLOAD_TRANSCRIPTS = os.getenv("UPLOAD_TRANSCRIPTS", "true").lower() in ("1", "true", "yes")
UPLOAD_RECORDINGS = os.getenv("UPLOAD_RECORDINGS", "true").lower() in ("1", "true", "yes")

# Turn detection parameters
TURN_DETECTION_MIN_ENDPOINTING_DELAY = float(os.getenv("TURN_DETECTION_MIN_ENDPOINTING_DELAY", "0.5"))
TURN_DETECTION_MIN_SILENCE_DURATION = float(os.getenv("TURN_DETECTION_MIN_SILENCE_DURATION", "0.5"))

async def _speak_status_update(ctx: RunContext, message: str, delay: float = 0.3):
    """Speak a brief status update before performing an action."""
    await asyncio.sleep(delay)
    await ctx.session.say(message, allow_interruptions=True)
    await asyncio.sleep(0.2)  # Brief pause after speaking

def get_gcs_client():
    """Initialize GCS client using base64-encoded service account JSON."""
    if not GCP_KEY_B64:
        raise RuntimeError("Missing GCP_SERVICE_ACCOUNT_KEY_BASE64 env var")
    
    try:
        decoded = base64.b64decode(GCP_KEY_B64).decode("utf-8")
        key_json = json.loads(decoded)
    except Exception as e:
        raise RuntimeError(f"Invalid base64 GCP key: {e}")
    
    # Validate required fields
    for req in ("project_id", "client_email", "private_key"):
        if req not in key_json:
            raise RuntimeError(f"GCP key missing required field: {req}")
    
    credentials = service_account.Credentials.from_service_account_info(key_json)
    client = storage.Client(credentials=credentials, project=key_json.get("project_id"))
    return client

async def send_status_to_backend(
    call_id: str,
    status: str,
    user_id: int = None,
    error_details: dict = None
):
    """Send status with GUARANTEED delivery"""
    payload = {
        "call_id": call_id,
        "status": status,
        "user_id": user_id,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    if status == "failed" and error_details:
        payload["error_details"] = error_details
    
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{BACKEND_API_URL}/api/agent/report-event",
                    json=payload
                )
                if response.status_code == 200:
                    logger.info(f"Status '{status}' sent for {call_id}")
                    return
        except Exception as e:
            if attempt == 2:
                logger.error(f" Failed to send status after 3 attempts: {e}")
            else:
                await asyncio.sleep(0.5)

    
class SimpleOutboundCaller(Agent):
    def __init__(self, *, call_context: str, dial_info: dict[str, Any]):
        self.user_id = dial_info.get("user_id")
        self.caller_name = dial_info.get("caller_name", "our office")
        self.caller_email = dial_info.get("caller_email")
        self.phone_number = dial_info.get("phone_number")
        self.agent_name = dial_info.get("agent_name","Paul")
        
        #  NEW: Get complete system prompt from metadata (passed from backend)
        system_prompt = dial_info.get("system_prompt")
        
        #  Fallback: Use hardcoded prompt if not provided
        if not system_prompt:
            logger.warning(" No system_prompt in metadata, using hardcoded fallback")
            system_prompt = self._build_fallback_prompt(call_context)
            logger.info(f" Using fallback prompt ({len(system_prompt)} chars)")
        
        logger.info(f"🤖 Initializing agent '{self.agent_name}' with prompt ({len(system_prompt)} chars)")
        
        # Pass to parent Agent class
        super().__init__(instructions=system_prompt)
        
        self.participant: rtc.RemoteParticipant | None = None
        self.dial_info = dial_info
        self.sip_call_id: str | None = None
        self.appointments_cache = []
        self.attendee_name: str | None = None
        self.egress_id: str | None = None
        self.recording_url: str | None = None
        self.recording_blob_path: str | None = None

    def _build_fallback_prompt(self, call_context: str) -> str:
        """
        Hardcoded fallback prompt when system_prompt is not provided in metadata.
        This ensures the agent can still function on LiveKit cloud without local imports.
        """
        return f"""You are {self.agent_name}, an AI assistant that makes phone calls to businesses on behalf of clients to book appointments and reservations.

                ### IDENTITY & ROLE

                #### WHO YOU ARE:
                - You are {self.agent_name}, a professional AI assistant
                - You represent your client: {self.caller_name}
                - You are the CUSTOMER calling the business
                - You are NOT affiliated with the business you're calling

                #### WHO YOU'RE CALLING:
                - A business employee (receptionist, host, scheduler)
                - Someone who has authority to book appointments
                - They work AT the business you're calling

                #### YOUR MISSION:
                {call_context}

                ### CONVERSATION PROTOCOL - MANDATORY SEQUENCE

                #### STEP 1: INTRODUCTION [REQUIRED - ALWAYS START HERE]
                **Template:** "Hi! This is {self.agent_name} calling on behalf of {self.caller_name}. How are you doing today?"

                **Rules:**
                - Use this exact greeting structure
                - Always mention you're calling on behalf of your client
                - Brief pleasantry to establish rapport
                - Wait for their response

                #### STEP 2: STATE PURPOSE [REQUIRED]
                **Template:** "I'm calling to [book an appointment/make a reservation] for {self.caller_name}."

                Then provide specifics:
                - What service/appointment type is needed
                - Any preferences (time of day, specific provider, etc.)
                - Duration if relevant

                **Rules:**
                - Be clear and direct about why you're calling
                - Don't assume they know why you're calling
                - Provide enough detail for them to help you

                #### STEP 3: LISTEN & GATHER OPTIONS [REQUIRED]
                **Actions:**
                - Let them propose available dates/times
                - Ask clarifying questions if needed
                - Take note of their suggestions

                **Rules:**
                - Don't interrupt
                - Acknowledge what they say ("okay", "got it", "I see")
                - If they ask what times work for you, proceed to Step 4

                #### STEP 4: CHECK AVAILABILITY [CRITICAL - MANDATORY]
                When they suggest a specific date/time OR when you need to propose times:

                **ACTION A - They suggest a time:**
                1. Say: "Let me check if that works for us..."
                2. IMMEDIATELY call: check_availability(date, time)
                3. Wait for result
                4. If AVAILABLE: "Perfect! That time works great for us."
                5. If NOT AVAILABLE: "Actually, we already have something at that time. Let me see what slots we have open..."
                Then call: get_available_times(date_range)

                **ACTION B - You need to suggest times:**
                1. Say: "Let me see what slots we have open..."
                2. Call: get_available_times(date_range)
                3. Review results
                4. Propose 2-3 options: "We're available at [time1], [time2], or [time3]. Do any of those work?"

                **Rules:**
                - NEVER agree to a time without checking availability first
                - ALWAYS use the check_availability tool when a specific time is proposed
                - Say a filler phrase before using any tool
                - Wait for tool response before continuing conversation

                #### STEP 5: CONFIRM & BOOK [REQUIRED]
                Once you have mutual agreement on a date/time that's been verified as available:

                1. Verbally confirm: "So we're all set for [day of week], [date] at [time]. Is that correct?"
                2. Wait for their confirmation
                3. Call: book_appointment(date, time, service_type, business_name, notes)
                4. Confirm aloud: "Perfect! I've booked {self.caller_name} for [date] at [time]. Thank you!"

                **Required information for booking:**
                - Date (YYYY-MM-DD format)
                - Time (HH:MM in 24-hour format)
                - Service type/purpose
                - Business name
                - Any special instructions (in notes field)

                **Rules:**
                - Only book after BOTH you and business confirm the time
                - Only book times that check_availability confirmed as free
                - Include all relevant details in the booking
                - Confirm the booking verbally after tool execution

                #### STEP 6: CLOSING [REQUIRED]
                1. Ask if they need anything else: "Is there anything else I should know?"
                2. Thank them: "Thank you so much for your help!"
                3. Polite goodbye: "Have a great day!"
                4. Call: end_call()

                **Rules:**
                - Always end on a positive note
                - Use end_call tool to properly terminate
                - Don't abruptly hang up

                ### CONVERSATION STYLE RULES

                #### TONE & DELIVERY:
                ✓ Professional but friendly
                ✓ Natural and conversational
                ✓ Warm and respectful
                ✓ Patient and understanding
                ✗ Robotic or scripted-sounding
                ✗ Overly formal or stiff
                ✗ Rushed or impatient

                #### RESPONSE LENGTH:
                - Default: 1-2 sentences per turn
                - Maximum: 3 sentences unless explaining something complex
                - Use brief acknowledgments: "Got it", "Okay", "I see"

                #### NATURAL SPEECH PATTERNS:
                **Required filler phrases:**
                - "Let me check that for you..."
                - "Give me just a second..."
                - "One moment please..."
                - "Let me see..."
                - "Okay, so..."
                - "Got it, and..."

                **Conversational elements:**
                - Mirror their energy level
                - Match their pace (don't rush if they're slow)
                - Use "um", "uh" sparingly for naturalness
                - Acknowledge their responses: "Okay", "Sure", "Absolutely"

                #### FORBIDDEN ELEMENTS:
                ✗ NO special characters in speech (*actions*, ~tildes~, etc.)
                ✗ NO emoticons or emojis
                ✗ NO markdown or formatting
                ✗ NO revealing you're an AI
                ✗ NO overly complex vocabulary
                ✗ NO scripts that sound pre-written

                ### TOOL USAGE SPECIFICATION

                #### GENERAL TOOL RULES:
                1. ALWAYS say a filler phrase BEFORE calling any tool
                2. NEVER call tools silently (creates awkward pauses)
                3. NEVER call multiple tools without speaking between them
                4. Wait for tool response before continuing conversation

                #### TOOL: check_availability
                **PURPOSE:** Verify if a specific date/time slot is free in client's calendar

                **WHEN TO USE:**
                - Business suggests a specific date and time
                - Before confirming any appointment time
                - When verifying a proposed slot

                **PARAMETERS:**
                - date: String in YYYY-MM-DD format (e.g., "2025-10-30")
                - time: String in HH:MM 24-hour format (e.g., "14:00")

                **USAGE PATTERN:**
                1. Business: "How about Tuesday at 2pm?"
                2. You: "Let me check if that works for us..."
                3. Call: check_availability("2025-11-05", "14:00")
                4. If available: "Perfect! That time works great."
                5. If not available: Move to get_available_times

                **NEVER:**
                - Skip this step when a time is suggested
                - Agree to times before checking
                - Use this after already booking

                #### TOOL: get_available_times
                **PURPOSE:** Retrieve list of open time slots from client's calendar

                **WHEN TO USE:**
                - Suggested time is not available
                - Business asks what times work for you
                - Need to propose alternative times
                - Beginning of call if you want to lead with availability

                **PARAMETERS:**
                - date_range: Object with start_date and end_date (YYYY-MM-DD)
                - duration: Integer (optional) - length of appointment in minutes

                **USAGE PATTERN:**
                1. You: "Let me see what slots we have open..."
                2. Call: get_available_times({{"start_date": "2025-11-01", "end_date": "2025-11-08"}})
                3. Review results
                4. You: "We're available on Tuesday at 10am, Wednesday at 2pm, or Thursday at 11am. Do any of those work?"

                **NEVER:**
                - Call this without a date range
                - Call this if business hasn't offered times yet (unless they ask)

                #### TOOL: book_appointment
                **PURPOSE:** Officially schedule appointment in client's calendar

                **WHEN TO USE:**
                - After BOTH parties agree on a date/time
                - After check_availability confirms slot is free
                - Before ending the call

                **PARAMETERS (ALL REQUIRED):**
                - date: String (YYYY-MM-DD)
                - time: String (HH:MM in 24-hour)
                - service_type: String (what the appointment is for)
                - business_name: String (name of the business)
                - notes: String (any special instructions or details)

                **USAGE PATTERN:**
                1. Mutual agreement reached
                2. You: "Perfect! Let me get that scheduled for you..."
                3. Call: book_appointment(
                    date="2025-11-05",
                    time="14:00",
                    service_type="Dental Cleaning",
                    business_name="Bright Smiles Dental",
                    notes="First visit, bring insurance card"
                )
                4. You: "All set! I've booked {self.caller_name} for November 5th at 2pm."

                **NEVER:**
                - Book without confirming availability first
                - Book without mutual agreement
                - Book then check availability (wrong order)
                - Book multiple times for same appointment

                #### TOOL: end_call
                **PURPOSE:** Properly terminate the phone call

                **WHEN TO USE:**
                - Appointment successfully booked and confirmed
                - Business declines/can't accommodate
                - Reached voicemail and left message
                - Conversation naturally concluded

                **PARAMETERS:** None

                **USAGE PATTERN:**
                1. Complete your final statement
                2. You: "Thank you so much! Have a great day!"
                3. Call: end_call()

                **NEVER:**
                - End call mid-conversation
                - End without saying goodbye
                - Forget to call this tool

                #### TOOL: detected_answering_machine
                **PURPOSE:** Signal that voicemail was reached

                **WHEN TO USE:**
                - Automated message plays
                - Hear a beep indicating voicemail
                - Clear indication it's not a live person

                **PARAMETERS:**
                - left_message: Boolean (true if you left a message)

                **USAGE PATTERN:**
                1. Detect voicemail
                2. Leave brief message: "Hi, this is {self.agent_name} calling for {self.caller_name} about booking an appointment. We'll try calling back later. Thank you!"
                3. Call: detected_answering_machine(left_message=true)
                4. Call: end_call()

                ### DATE & TIME FORMATTING STANDARDS

                #### INTERNAL FORMAT (for tools):
                - Date: YYYY-MM-DD (e.g., "2025-10-30")
                - Time: HH:MM in 24-hour (e.g., "14:00" for 2 PM, "09:30" for 9:30 AM)

                #### SPOKEN FORMAT (in conversation):
                - Date: "Tuesday, October 30th" or "October 30th"
                - Time: "2 PM" or "2 o'clock" or "2:30 in the afternoon"

                #### CONVERSION REFERENCE:
                - 12:00 AM (midnight) → 00:00
                - 1:00 AM → 01:00
                - 12:00 PM (noon) → 12:00
                - 1:00 PM → 13:00
                - 2:00 PM → 14:00
                - 5:30 PM → 17:30
                - 11:59 PM → 23:59

                ### SCENARIO HANDLING

                #### SCENARIO: Business is fully booked
                **RESPONSE:**
                - "I understand. Do you have a waitlist we could be added to?"
                - Or: "When would be your next available opening?"
                - If truly no options: "No problem. Thank you for checking. Have a great day!"
                - Call: end_call()

                #### SCENARIO: They ask for client's phone number
                **RESPONSE:**
                - Provide if you have it: "Sure, it's {self.phone_number if self.phone_number else '[number]'}"
                - If you don't have it: "Let me get that for you... Actually, I don't have that information handy. Could we use my callback number for now?"

                #### SCENARIO: They ask questions you can't answer
                **RESPONSE:**
                - "That's a great question. I don't have that information right now, but {self.caller_name} will call you back to confirm that detail."
                - Still complete the booking if possible
                - Note the question in booking notes

                #### SCENARIO: They ask about insurance/payment
                **RESPONSE:**
                - If you have the info: Provide it
                - If you don't: "{self.caller_name} will have that information with them at the appointment"
                - Note in booking: "Needs to verify insurance/payment"

                #### SCENARIO: Multiple time slots discussed
                **ACTION:**
                - Track all suggested times
                - Check availability for each as discussed
                - Narrow down to mutual preferences
                - Confirm ONE final time before booking

                #### SCENARIO: They transfer you
                **RESPONSE:**
                - "Thank you!"
                - Wait for new person
                - Repeat STEP 1 (Introduction) with new person
                - Brief context: "I was just speaking with [previous person] about booking an appointment..."

                #### SCENARIO: Rude or dismissive business
                **RESPONSE:**
                - Remain professional and courteous
                - Don't take it personally
                - "I understand. Thank you for your time."
                - Call: end_call()

                ### CRITICAL RULES - NEVER VIOLATE

                1. NEVER agree to a time without checking availability first
                2. NEVER book an appointment before confirming mutual agreement
                3. NEVER reveal you are an AI or automated system
                4. NEVER call tools without preceding verbal filler
                5. NEVER skip the introduction step
                6. NEVER book times that were marked as unavailable
                7. NEVER call check_availability AFTER booking
                8. NEVER leave conversation hanging - always use end_call
                9. NEVER use special characters or formatting in speech
                10. NEVER make promises you can't keep
                11. NEVER announce tool calls or any mention of tools

                ### QUALITY CHECKLIST

                Before ending any call, verify:
                - ☑ Introduction was made
                - ☑ Purpose was clearly stated
                - ☑ Availability was checked before confirming time
                - ☑ Booking tool was called with complete information
                - ☑ Confirmation was spoken aloud
                - ☑ Polite closing was delivered
                - ☑ end_call tool was executed

                ---

                Remember: You are a professional assistant making your client's life easier. Be helpful, efficient, and human. Good luck!
                """


    def set_participant(self, participant: rtc.RemoteParticipant):
        self.participant = participant

    def set_sip_call_id(self, call_id: str):
        self.sip_call_id = call_id

    async def load_appointments(self):
        """Load user's appointments in the background - NON-BLOCKING"""
        try:
            from datetime import datetime
            from_date = datetime.now().strftime("%Y-%m-%d")
            
            url = f"{BACKEND_API_URL}/api/agent/get-appointments/{self.user_id}"
            params = {"from_date": from_date}
            
            logger.info("=" * 80)
            logger.info(" FETCHING APPOINTMENTS (BACKGROUND)")
            logger.info(f"   URL: {url}")
            logger.info(f"   Params: {params}")
            logger.info("=" * 80)
            
            #  Use shorter timeout - we don't want to wait forever
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),  # Reduced from 30s to 10s
                follow_redirects=True,
                verify=True,
            ) as client:
                logger.info("📡 Sending GET request...")
                response = await client.get(url, params=params)
                
                logger.info(f"📥 Response status: {response.status_code}")
                
                if response.status_code == 200:
                    data = response.json()
                    self.appointments_cache = data.get("appointments", [])
                    logger.info(f" Loaded {len(self.appointments_cache)} appointments")
                    
                    if len(self.appointments_cache) > 0:
                        logger.info(f"📋 Sample: {self.appointments_cache[0]}")
                else:
                    logger.warning(f" Non-200 status: {response.status_code}")
                    self.appointments_cache = []
                    
        except httpx.ConnectError as e:
            logger.error(f" CONNECTION ERROR")
            logger.error(f"   Cannot reach: {BACKEND_API_URL}")
            logger.error(f"   Error: {e}")
            self.appointments_cache = []
            
        except httpx.ReadTimeout:
            logger.error(f" READ TIMEOUT (>10s)")
            logger.error(f"   Backend took too long to respond")
            logger.error(f"   Continuing with empty cache")
            self.appointments_cache = []
            
        except Exception as e:
            logger.error(f" ERROR: {type(e).__name__}: {e}")
            self.appointments_cache = []


    @function_tool()
    async def check_availability(
        self,
        ctx: RunContext,
        appointment_date: str,
        start_time: str,
        end_time: str = None
    ):
        """
        Check if YOUR CLIENT is available at this time.
        Will wait for appointments to load if still loading.
        """
        await _speak_status_update(ctx, "Let me check if that works for us...")
        
        # ✅ Import time object for comparison
        from datetime import datetime, timedelta, time

        try:
            # Auto-calculate end_time if not provided
            if not end_time:
                start_dt = datetime.strptime(start_time, "%H:%M")
                end_dt = start_dt + timedelta(hours=1)
                end_time = end_dt.strftime("%H:%M")
            
            # ✅ Convert proposed times to time objects for correct comparison
            try:
                proposed_start_t = datetime.strptime(start_time, "%H:%M").time()
                proposed_end_t = datetime.strptime(end_time, "%H:%M").time()
            except ValueError:
                logger.error(f"❌ Invalid proposed time format: {start_time}-{end_time}")
                # Tell the agent the format is wrong so it can re-ask
                return {
                    "available": False, 
                    "message": "The time format provided was invalid. Please ensure it's HH:MM 24-hour format."
                }

            logger.info(f"🔍 Checking availability: {appointment_date} {start_time}-{end_time}")
            logger.info(f"📋 Cached appointments: {len(self.appointments_cache)}")
            
            # ✅ If cache is empty, try ONE quick API call as fallback
            if len(self.appointments_cache) == 0:
                logger.warning("⚠️ Cache empty - attempting quick API call...")
                try:
                    from datetime import datetime as dt
                    from_date = dt.now().strftime("%Y-%m-%d")
                    url = f"{BACKEND_API_URL}/api/agent/get-appointments/{self.user_id}"
                    params = {"from_date": from_date}
                    
                    # ✅ Very short timeout - don't wait long
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        response = await client.get(url, params=params)
                        if response.status_code == 200:
                            data = response.json()
                            self.appointments_cache = data.get("appointments", [])
                            logger.info(f"✅ Loaded {len(self.appointments_cache)} appointments via fallback")
                except Exception as e:
                    logger.error(f"❌ Fallback API call failed: {e}")
                    # Continue with empty cache
            
            # Check for conflicts in cache
            has_conflict = False
            conflicting_appointment = None
            
            # ✅ FIX: Use time objects for comparison, not strings
            for apt in self.appointments_cache:
                if apt["date"] != appointment_date:
                    continue
                
                try:
                    # Robustly parse HH:MM or H:MM from cache
                    apt_start_h, apt_start_m = map(int, str(apt["start_time"]).split(':'))
                    apt_end_h, apt_end_m = map(int, str(apt["end_time"]).split(':'))
                    
                    apt_start_t = time(apt_start_h, apt_start_m)
                    apt_end_t = time(apt_end_h, apt_end_m)

                except Exception as e:
                    logger.warning(f"⚠️ Could not parse cached time: {apt}. Skipping. Error: {e}")
                    continue

                # ✅ Correct logical comparison for time ranges
                # Conflict exists if:
                # (Proposed start is before cached end) AND (Proposed end is after cached start)
                if (proposed_start_t < apt_end_t and proposed_end_t > apt_start_t):
                    has_conflict = True
                    conflicting_appointment = apt
                    logger.info(f"⚠️ Conflict found: {apt}")
                    break
            
            if has_conflict:
                return {
                    "available": False,
                    "message": f"Your client already has '{conflicting_appointment['title']}' at {appointment_date} from {conflicting_appointment['start_time']} to {conflicting_appointment['end_time']}. Suggest a different time."
                }
            else:
                logger.info(f"✅ Time slot is available")
                return {
                    "available": True,
                    "message": f"Your client is free on {appointment_date} at {start_time}. You can book this time."
                }
                    
        except Exception as e:
            logger.error(f"❌ Error checking availability: {e}")
            import traceback
            traceback.print_exc()
            # ✅ Assume available if error (better UX than blocking)
            return {
                "available": True,
                "message": "I couldn't verify the schedule, but let's proceed with this time.",
                "warning": "Schedule verification unavailable"
            }
        

    @function_tool()
    async def get_available_times(self, ctx: RunContext, appointment_date: str):
        """
        Get YOUR CLIENT's booked times for a specific date to see when they're free.
        
        Args:
            appointment_date: Date in YYYY-MM-DD format (e.g., 2025-10-30)
        """
        # Speak status update before fetching
        await _speak_status_update(ctx, "One moment, let me check our schedule...")
        
        try:
            logger.info(f" Getting booked times for: {appointment_date}")
            
            booked_slots = [
                apt for apt in self.appointments_cache
                if apt["date"] == appointment_date
            ]
            
            if not booked_slots:
                return {
                    "date": appointment_date,
                    "message": f"Your client {self.caller_name} is completely free on {appointment_date}. Any time works!",
                    "booked_slots": []
                }
            
            slots_info = ", ".join([f"{apt['start_time']}-{apt['end_time']}" for apt in booked_slots])
            
            return {
                "date": appointment_date,
                "message": f"Your client is already booked at: {slots_info}. They're free at other times.",
                "booked_slots": booked_slots
            }
        except Exception as e:
            logger.error(f"Error getting available times: {e}")
            return {"error": "Unable to fetch available times"}

    @function_tool()
    async def book_appointment(
        self,
        ctx: RunContext,
        appointment_date: str,
        start_time: str,
        end_time: str,
        attendee_name: str = "Service Provider",
        title: str = "Appointment",
        notes: str | None = None
    ):
        """
        Book the appointment for YOUR CLIENT after confirming they're available.
        
        Args:
            appointment_date: Date in YYYY-MM-DD format (e.g., 2025-10-30)
            start_time: Start time in HH:MM 24-hour format (e.g., 14:00)
            end_time: End time in HH:MM 24-hour format (e.g., 15:00)
            attendee_name: Name of the business/person you're booking with
            title: Brief title for the appointment
            notes: Any special instructions (e.g., "bring X-rays", "fast for 8 hours")
        """
        # Speak status update before booking
        await _speak_status_update(ctx, "Perfect, let me get that booked for you...")
        
        try:
            self.attendee_name = attendee_name
            
            logger.info(f" Booking appointment: {appointment_date} {start_time}-{end_time}")
            
            url = f"{BACKEND_API_URL}/api/agent/book-appointment"
            logger.info(f" Booking URL: {url}")
            payload = {
                "user_id": self.user_id,
                "appointment_date": appointment_date,
                "start_time": start_time,
                "end_time": end_time,
                "attendee_name": attendee_name,
                "title": title,
                "description": f"Appointment booked on behalf of {self.caller_name}",
                "organizer_name": self.caller_name,
                "organizer_email": self.caller_email,
                "notes": notes or ""
            }
            
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, json=payload)
                logger.info(f" Response status: {response.status_code}")  #  Add this
                logger.info(f" Response body: {response.text}")
                data = response.json()
            
            if data.get("success"):
                self.appointments_cache.append({
                    "date": appointment_date,
                    "start_time": start_time,
                    "end_time": end_time,
                    "title": title
                })
                
                logger.info(f" Appointment booked successfully")
                
                return {
                    "success": True,
                    "message": f"Successfully booked! {self.caller_name} is confirmed for {appointment_date} at {start_time}.",
                    "appointment_id": data.get("appointment_id")
                }
            else:
                return {
                    "success": False,
                    "message": data.get("message", "Failed to book appointment")
                }
                
        except Exception as e:
            logger.error(f" Error booking appointment: {e}")
            logger.error(f"📋 Full traceback: {traceback.format_exc()}")
            logger.error(f"Error booking appointment: {e}")
            return {"success": False, "error": "Unable to book appointment"}

    @function_tool()
    async def end_call(self, ctx: RunContext):
        """End the phone call politely and hang up. Use this when the conversation is complete."""
        logger.info(" Ending call...")
        try:
            await ctx.wait_for_playout()
        except:
            pass

        job_ctx = get_job_context()
        if job_ctx:
            try:
                await job_ctx.api.room.delete_room(api.DeleteRoomRequest(room=job_ctx.room.name))
                logger.info(" Room deleted")
                await send_status_to_backend(job_ctx.room.name, "completed", self.user_id)
            except Exception as e:
                logger.warning(f" Failed to delete room: {e}")
        
        try:
            ctx.shutdown(reason="Call ended by agent")
        except:
            pass

async def entrypoint(ctx: JobContext):
    logger.info(f" Connecting to room {ctx.room.name}")
    
    # Parse metadata
    metadata_str = ctx.job.metadata or "{}"
    try:
        dial_info = json.loads(metadata_str)
    except json.JSONDecodeError:
        logger.error("Invalid metadata JSON")
        return

    phone_number = dial_info.get("phone_number")
    call_context = dial_info.get("call_context", "booking an appointment")
    user_id = dial_info.get("user_id")
    
    #  Extract voice and language from metadata
    voice_id = dial_info.get("voice_id", os.getenv("ELEVENLABS_VOICE_ID"))
    voice_name = dial_info.get("voice_name", "default")
    language = dial_info.get("language", "en")
    
    logger.info(f"🎤 Voice: {voice_name} (ID: {voice_id})")
    logger.info(f"🌐 Language: {language}")
    
    has_system_prompt = "system_prompt" in dial_info
    if has_system_prompt:
        prompt_length = len(dial_info.get("system_prompt", ""))
        logger.info(f" System prompt received in metadata ({prompt_length} chars)")
    else:
        logger.warning(" No system prompt in metadata - will use fallback")
    
    if not phone_number:
        logger.error(" Missing phone_number in metadata")
        return

    #  Pass entire dial_info to agent (includes system_prompt)
    agent = SimpleOutboundCaller(call_context=call_context, dial_info=dial_info)
    
    #  Use MultilingualModel for all languages (supports English too)
    turn_detector = MultilingualModel()
    
    # Create session with DYNAMIC voice, language, and turn detection
    session = AgentSession(
        llm=openai.LLM(
            model="gpt-4.1-mini",
            api_key=os.getenv("OPENAI_API_KEY")
        ),
        stt=deepgram.STT(
            api_key=os.getenv("DEEPGRAM_API_KEY"),
            model="nova-3",          
        ),
        tts=elevenlabs.TTS(
            api_key=os.getenv("ELEVENLABS_API_KEY"),
            model="eleven_flash_v2_5",
            voice_id="1SM7GgM6IMuvQlz2BwM3"
        ),
        vad=silero.VAD.load(min_silence_duration=0.05),
        turn_detection=turn_detector,       
        min_endpointing_delay=0.05,
    )


    async def upload_transcript():
        """Upload transcript to GCS and send metadata to backend"""
        if not UPLOAD_TRANSCRIPTS:
            logger.info("⏭️ Transcript upload disabled")
            return

        try:
            # Generate transcript JSON
            transcript_obj = session.history.to_dict() if hasattr(session, 'history') else {"messages": []}
            transcript_json = json.dumps(transcript_obj, indent=2)
            
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            safe_phone = phone_number.replace("+", "").replace("-", "").replace(" ", "")
            blob_name = f"transcripts/{ctx.room.name}_{safe_phone}_{ts}.json"

            # Upload to GCS
            gcs = get_gcs_client()
            bucket = gcs.bucket(GOOGLE_BUCKET_NAME)
            blob = bucket.blob(blob_name)
            blob.upload_from_string(transcript_json, content_type="application/json")
            
            # Generate signed URL (optional - backend can use blob path directly)
            signed_url = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(hours=24),
                method="GET"
            )
            
            logger.info(f" Transcript uploaded: {blob_name}")

            #  Build payload with BOTH URLs and blob paths
            payload = {
                "user_id": agent.user_id,
                "call_id": ctx.room.name,
                "transcript_url": signed_url,
                "transcript_blob": blob_name,  # ← Backend uses this for direct GCS access
                "recording_url": agent.recording_url,  # ← Optional (may be None)
                "recording_blob": agent.recording_blob_path,  # ← CRITICAL: Backend needs this!
                "uploaded_at": ts
            }
            
            logger.info(f"📤 Sending call data to backend:")
            logger.info(f"   Call ID: {ctx.room.name}")
            logger.info(f"   Transcript blob: {blob_name}")
            logger.info(f"   Recording blob: {agent.recording_blob_path}")
            
            # Send to backend with retries
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    async with httpx.AsyncClient(timeout=60.0) as c:
                        response = await c.post(
                            f"{BACKEND_API_URL}/api/agent/save-call-data", 
                            json=payload
                        )
                        if response.status_code == 200:
                            logger.info(" Call data sent to backend")
                            break
                        else:
                            logger.warning(f" Backend returned {response.status_code}")
                            logger.warning(f"   Response: {response.text[:200]}")
                except httpx.ReadTimeout:
                    if attempt < max_retries - 1:
                        logger.warning(f" Timeout on attempt {attempt + 1}, retrying...")
                        await asyncio.sleep(2)
                    else:
                        logger.error(f" Backend timeout after {max_retries} attempts")
                except Exception as e:
                    logger.error(f" Backend request failed: {e}")
                    break

        except Exception as e:
            logger.error(f" Transcript upload failed: {e}")
            import traceback
            traceback.print_exc()
    
    ctx.add_shutdown_callback(upload_transcript)
    
    # ========== STATUS 1: INITIALIZED ==========
    await ctx.connect()
    await send_status_to_backend(ctx.room.name, "initialized", user_id)

    # ============== START RECORDING ==============
    if UPLOAD_RECORDINGS:
        try:
            safe_phone = phone_number.replace("+", "").replace("-", "").replace(" ", "")
            ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            recording_filename = f"recordings/{ctx.room.name}_{safe_phone}_{ts}.ogg"
            
            #  STORE BLOB PATH IN AGENT (Critical for backend access)
            agent.recording_blob_path = recording_filename
            
            logger.info(f"🎙️ Starting recording: {recording_filename}")

            decoded_creds = base64.b64decode(GCP_KEY_B64).decode("utf-8")
            
            req = api.RoomCompositeEgressRequest(
                room_name=ctx.room.name,
                audio_only=True,
                file_outputs=[
                    api.EncodedFileOutput(
                        file_type=api.EncodedFileType.OGG,
                        filepath=recording_filename,
                        gcp=api.GCPUpload(
                            bucket=GOOGLE_BUCKET_NAME,
                            credentials=decoded_creds
                        )
                    )
                ],
            )
            
            lkapi = api.LiveKitAPI(
                url=os.getenv("LIVEKIT_URL", "").replace("wss://", "https://"),
                api_key=os.getenv("LIVEKIT_API_KEY"),
                api_secret=os.getenv("LIVEKIT_API_SECRET"),
            )
            
            egress_resp = await lkapi.egress.start_room_composite_egress(req)
            agent.egress_id = egress_resp.egress_id
            
            #  Build recording URL (optional - backend can access directly via blob path)
            agent.recording_url = f"https://storage.googleapis.com/{GOOGLE_BUCKET_NAME}/{recording_filename}"
            
            logger.info(f" Recording started (egress_id: {agent.egress_id})")
            logger.info(f"   Recording blob path: {agent.recording_blob_path}")
            
            #  IMMEDIATELY notify backend of recording blob path
            # This allows backend to start preparing to download once recording finishes
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(
                        f"{BACKEND_API_URL}/api/update-call-recording",
                        json={
                            "call_id": ctx.room.name,
                            "recording_blob": recording_filename,
                            "recording_url": agent.recording_url
                        }
                    )
                    logger.info(f" Recording blob path sent to backend")
            except Exception as e:
                logger.warning(f" Could not send recording path to backend: {e}")
                # Not critical - will be sent again in upload_transcript()
            
            await lkapi.aclose()
            
        except Exception as e:
            logger.error(f" Failed to start recording: {e}")
            import traceback
            traceback.print_exc()
    else:
        logger.info("⏭️ Recording disabled")

    # Load appointments (non-blocking)
    asyncio.create_task(agent.load_appointments())

    # Background audio
    background_audio = BackgroundAudioPlayer(
        thinking_sound=[
            AudioConfig(BuiltinAudioClip.OFFICE_AMBIENCE, volume=0.5),
            AudioConfig(BuiltinAudioClip.KEYBOARD_TYPING2, volume=0.8),
        ],
    )
    await background_audio.start(room=ctx.room, agent_session=session)

    try:
        # ========== STATUS 2: DIALING ==========
        logger.info(f" Dialing {phone_number}...")
        asyncio.create_task(
            send_status_to_backend(ctx.room.name, "dialing", user_id)
        )
        
        sip_response = await ctx.api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=ctx.room.name,
                sip_trunk_id=OUTBOUND_TRUNK_ID,
                sip_call_to=phone_number,
                participant_identity=f"sip-{phone_number}",
                wait_until_answered=True,
            )
        )
        
        agent.set_sip_call_id(sip_response.sip_call_id)
        logger.info(f" SIP call created: {sip_response.sip_call_id}")

        participant = await ctx.wait_for_participant(identity=f"sip-{phone_number}")
        agent.set_participant(participant)
        logger.info(f" Participant joined: {participant.identity}")

        # ========== STATUS 3: CONNECTED ==========
        #  Set started_at timestamp when call connects
        try:
            started_at = datetime.now(timezone.utc).isoformat()
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{BACKEND_API_URL}/api/update-call-started",
                    json={
                        "call_id": ctx.room.name,
                        "started_at": started_at
                    }
                )
                logger.info(f" Started_at timestamp set: {started_at}")
        except Exception as e:
            logger.warning(f" Could not set started_at: {e}")
        
        asyncio.create_task(
            send_status_to_backend(ctx.room.name, "connected", user_id)
        )

        # NOW start session (AFTER status sent)
        session_task = asyncio.create_task(
            session.start(agent=agent, room=ctx.room, room_input_options=RoomInputOptions())
        )

        await asyncio.sleep(0.5)
        await session.say(
            f"Hi! This is paul calling on behalf of {agent.caller_name}. How are you doing today?",
            allow_interruptions=True
        )

        await session_task
        logger.info(" Full session completed")

    except api.TwirpError as e:
        # ========== STATUS 4: UNANSWERED ==========
        logger.error(f" SIP dial failed: {e.message}")
        
        await send_status_to_backend(
            ctx.room.name, 
            "unanswered", 
            user_id,
            error_details={"reason": "sip_failed", "error_message": e.message}
        )
        
        #  Update DB directly as backup
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{BACKEND_API_URL}/api/agent/save-call-data",
                    json={
                        "call_id": ctx.room.name,
                        "user_id": user_id,
                        "status": "unanswered",
                        "transcript_url": None,
                        "recording_url": None
                    }
                )
        except:
            pass
                
        ctx.shutdown()
        
    except Exception as e:
        logger.error(f" Unexpected error: {e}")
        
        await send_status_to_backend(
            ctx.room.name,
            "failed",
            user_id,
            error_details={
                "reason": "error",
                "error_message": str(e)
            }
        )
        
        import traceback
        traceback.print_exc()
        ctx.shutdown()

if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller",
        )
    )