from pydantic import BaseModel, EmailStr, Field, validator
from typing import List, Optional,Dict,Literal
from datetime import datetime


### =============== auth base model ====================

class UserRegister(BaseModel):
    username: str
    email: EmailStr
    password: str

class UserLogin(BaseModel):
    email: str  # We’ll use this to accept the username
    password: str

class UserOut(BaseModel):
    id: int
    username: str
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    created_at: datetime
    is_admin: bool = False #*

class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    user: UserOut

class UpdateUserProfileRequest(BaseModel):
    # user_id: int
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    current_password: Optional[str] = None
    new_password: Optional[str] = None    


class Assistant_Payload(BaseModel):
    objective: str
    context: str
    # caller_number: str
    caller_name: str
    caller_number: str
    caller_email: str
    outbound_number : str
    language : Literal['english', 'spanish']
    voice : str
    # outbound_number : str


class CallDetailsPayload(BaseModel):
    # user_id: int
    call_id: str
    voice_name : str
    # caller_email: EmailStr

class Assistant_Payload(BaseModel):
    outbound_number: str      # Phone number to dial
    caller_name: str          # Your name/company name
    caller_email: str         # Your email (for sending calendar invites)
    caller_number: str        # Your phone number
    # objective: str
    context: str
    language: str 
    voice: str 

class AddVoiceRequest(BaseModel):
    voice_name: str
    voice_id: str

class VoiceResponse(BaseModel):
    voice_name: str
    voice_id: str

class PromptCustomizationUpdate(BaseModel):
    system_prompt: str = Field(..., min_length=1, max_length=1000000)


class ContactUploadResponse(BaseModel):
    success: bool
    message: str
    stats: dict

class ContactsListResponse(BaseModel):
    contacts: list
    pagination: Optional[dict] = None



class ContactUploadStats(BaseModel):
    total_rows: int
    inserted: int
    duplicates: int = 0
    skipped: int = 0
    errors: int = 0

class ContactUploadResponse(BaseModel):
    success: bool
    message: str
    stats: ContactUploadStats



class BulkCallPayload(BaseModel):
    """
    Payload for bulk calling multiple phone numbers.
    """
    phone_numbers: List[str] = Field(..., min_items=1, description="List of phone numbers to call")
    caller_name: str = Field(..., min_length=1, description="Name of the caller")
    caller_email: str = Field(..., min_length=1, description="Email of the caller")
    context: str = Field(..., min_length=1, description="Call context/purpose")
    system_prompt: str = Field(..., min_length=1, description="The complete system prompt to use")
    voice: str = Field(default="david", description="Voice name (david, ravi, emily-british, etc.)")
    language: str = Field(default="en", description="Language code (en or es)")
    first_name: Optional[str] = Field(default=None, description="Fallback contact first name applied to all numbers")
    first_names: Optional[List[Optional[str]]] = Field(default=None, description="Per-number first names aligned with phone_numbers")
    email: Optional[str] = Field(default=None, description="Target contact email for calendar invites/SMS")
    category: Optional[str] = Field(default=None, description="Call category (e.g., 'appt_setting', 'lead_gen')")
    
    @validator('phone_numbers')
    def validate_phone_numbers(cls, v):
        if not v:
            raise ValueError("At least one phone number is required")
        
        # Clean and validate each number
        cleaned = []
        for num in v:
            # Remove spaces, dashes, etc.
            clean = ''.join(c for c in num if c.isdigit() or c == '+')
            if len(clean) < 10:
                raise ValueError(f"Invalid phone number: {num}")
            cleaned.append(clean)
        
        return cleaned

    @validator('first_names')
    def validate_first_names(cls, v, values, **kwargs):
        if v is None:
            return v
        phone_numbers = values.get('phone_numbers') or []
        if phone_numbers and len(v) not in (0, len(phone_numbers)):
            raise ValueError("first_names must match phone_numbers length or be omitted")
        # Normalize blanks to None
        return [name.strip() if name and name.strip() else None for name in v]

    @validator('category')
    def normalize_category(cls, v):
        if not v:
            return None
        v_clean = v.strip().lower()
        allowed = {"appt setting", "appt_setting", "appointment", "lead gen", "lead_gen", "leadgen"}
        if v_clean not in allowed:
            return v_clean  # keep flexible but normalized to lower
        return v_clean


class SingleCallPayload(BaseModel):
    """
    Payload for single call (backward compatibility).
    """
    outbound_number: str = Field(..., min_length=1, description="Phone number to call")
    caller_name: str = Field(..., min_length=1, description="Name of the caller")
    caller_email: str = Field(..., min_length=1, description="Email of the caller")
    context: str = Field(..., min_length=1, description="Call context/purpose")
    system_prompt: str = Field(..., min_length=1, description="The complete system prompt to use")
    voice: str = Field(default="david", description="Voice name")
    language: str = Field(default="en", description="Language code")
    first_name: Optional[str] = Field(default=None, description="Target contact first name to personalize the call")
    email: Optional[str] = Field(default=None, description="Target contact email")
    category: Optional[str] = Field(default=None, description="Call category (appt_setting, lead_gen, etc.)")


 
class CreatePromptRequest(BaseModel):
    """Request to create a new prompt"""
    prompt_name: str = Field(..., min_length=1, max_length=255, description="Name/heading for the prompt")
    system_prompt: str = Field(..., min_length=1, description="The actual prompt text")


class UpdatePromptRequest(BaseModel):
    """Request to update a prompt"""
    prompt_name: Optional[str] = Field(None, min_length=1, max_length=255)
    system_prompt: Optional[str] = Field(None, min_length=1)


class PromptResponse(BaseModel):
    """Response containing prompt data"""
    id: int
    user_id: int
    prompt_name: str
    system_prompt: str
    is_default: bool
    created_at: str
    updated_at: str


class BulkCallResponse(BaseModel):
    """Response for bulk calling"""
    success: bool
    message: str
    total_calls: int
    initiated_calls: List[dict] 
    failed_calls: List[dict] 

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8)