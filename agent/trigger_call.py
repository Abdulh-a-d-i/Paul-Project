import os
import asyncio
import json
import random
from dotenv import load_dotenv
from livekit import api
import os
import asyncio
import json
import random
from dotenv import load_dotenv
from livekit import api

load_dotenv()

# --- CONFIGURE YOUR CALL HERE ---
PHONE_NUMBER_TO_CALL = "+923451452451"  # IMPORTANT: Use a verified number for testing
CALL_CONTEXT = "You are a friendly assistant calling to check in and see how the user is doing today."
AGENT_NAME = "outbound-caller"  # Must match the name in the agent worker script
# Additional optional metadata for the agent (based on the agent's expectations)
USER_ID = 123  # Replace with actual user ID if available
CALLER_NAME = "Test Caller"  # Replace with actual caller name
CALLER_EMAIL = "test@example.com"  # Replace with actual email
SYSTEM_PROMPT = None  # Optional: Custom system prompt; falls back to hardcoded if None
VOICE_ID = None  # Optional: ElevenLabs voice ID
LANGUAGE = "en"  # Optional: Language code
# ------------------------------

async def main():
    print(f"Attempting to dispatch agent '{AGENT_NAME}' to call {PHONE_NUMBER_TO_CALL}...")

    # Connect to the LiveKit API
    lkapi = api.LiveKitAPI(
        url=os.getenv("LIVEKIT_URL").replace("wss://", "https://"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )

    # Prepare the metadata to send to the agent
    metadata = json.dumps({
        "phone_number": PHONE_NUMBER_TO_CALL,
        "call_context": CALL_CONTEXT,
        "user_id": USER_ID,
        "caller_name": CALLER_NAME,
        "caller_email": CALLER_EMAIL,
        "voice_id": VOICE_ID,
        "language": LANGUAGE,
    })

    # Create a unique room for this call
    room_name = f"outbound-call-{random.randint(1000, 9999)}"

    try:
        async with lkapi:
            # Dispatch the agent with the metadata
            dispatch = await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=AGENT_NAME,
                    room=room_name,
                    metadata=metadata,
                )
            )
        print(f"✅ Agent dispatched successfully for room '{room_name}'!")
        print(f"   Dispatch ID: {dispatch.id}")
        print("   Check agent logs with 'lk agent logs' to see progress.")
    except Exception as e:
        print(f"❌ Failed to dispatch agent: {e}")

if __name__ == "__main__":
    asyncio.run(main())
load_dotenv()

# --- CONFIGURE YOUR CALL HERE ---
PHONE_NUMBER_TO_CALL = "+923451452451"  # IMPORTANT: Use a verified number for testing
CALL_CONTEXT = "You are a friendly assistant calling to check in and see how the user is doing today."
AGENT_NAME = "simple-outbound-caller"  # Must match the name in the agent worker script
# ------------------------------

async def main():
    print(f"Attempting to dispatch agent '{AGENT_NAME}' to call {PHONE_NUMBER_TO_CALL}...")

    # Connect to the LiveKit API
    lkapi = api.LiveKitAPI(
        url=os.getenv("LIVEKIT_URL").replace("wss://", "https://"),
        api_key=os.getenv("LIVEKIT_API_KEY"),
        api_secret=os.getenv("LIVEKIT_API_SECRET"),
    )

    # Prepare the metadata to send to the agent
    metadata = json.dumps({
        "phone_number": PHONE_NUMBER_TO_CALL,
        "call_context": CALL_CONTEXT,
    })

    # Create a unique room for this call
    room_name = f"simple-outbound-call-{random.randint(1000, 9999)}"

    try:
        async with lkapi:
            # Dispatch the agent with the metadata
            dispatch = await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=AGENT_NAME,
                    room=room_name,
                    metadata=metadata,
                )
            )
        print(f"✅ Agent dispatched successfully for room '{room_name}'!")
        print(f"   Dispatch ID: {dispatch.id}")
        print("   Check agent logs with 'lk agent logs' to see progress.")
    except Exception as e:
        print(f"❌ Failed to dispatch agent: {e}")

if __name__ == "__main__":
    asyncio.run(main())