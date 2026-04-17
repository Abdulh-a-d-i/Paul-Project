import os
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request

from dotenv import load_dotenv

load_dotenv()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class GoogleCalendarService:
    """
    Wrapper for Google Calendar API operations.
    Handles authentication, token refresh, and common operations.
    """
    
    def __init__(self, credentials_dict: Dict):
        """
        Initialize with credentials dictionary from database.
        
        Args:
            credentials_dict: Dict with access_token, refresh_token, token_expiry, scopes
        """
        self.credentials = Credentials(
            token=credentials_dict['access_token'],
            refresh_token=credentials_dict.get('refresh_token'),
            token_uri='https://oauth2.googleapis.com/token',
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=credentials_dict.get('scopes', ['https://www.googleapis.com/auth/calendar'])
        )
        
        if credentials_dict.get('token_expiry'):
            expiry = credentials_dict['token_expiry']
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
            self.credentials.expiry = expiry

            if isinstance(expiry, datetime) and expiry.tzinfo:
                expiry = expiry.astimezone(timezone.utc).replace(tzinfo=None)
                
            self.credentials.expiry = expiry

        self.service = build('calendar', 'v3', credentials=self.credentials)
        
        self.service = build('calendar', 'v3', credentials=self.credentials)
        logger.info("✅ Google Calendar service initialized")

    def _refresh_if_needed(self):
        """Refresh token if expired."""
        if self.credentials and self.credentials.expired and self.credentials.refresh_token:
            try:
                self.credentials.refresh(Request())
                logger.info("🔄 Access token refreshed")
            except Exception as e:
                logger.error(f"❌ Token refresh failed: {e}")
                raise

    def list_events(
        self, 
        time_min: datetime, 
        time_max: datetime = None, 
        max_results: int = 100
    ) -> List[Dict]:
        """
        List events from primary calendar.
        
        Args:
            time_min: Minimum time (inclusive)
            time_max: Maximum time (exclusive)
            max_results: Maximum number of events to return
            
        Returns:
            List of formatted event dicts with: id, date, start_time, end_time, summary, description, location
        """
        self._refresh_if_needed()
        
        try:
            events_result = self.service.events().list(
                calendarId='primary',
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat() if time_max else None,
                maxResults=max_results,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            events = events_result.get('items', [])
            formatted_events = []
            
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                end = event['end'].get('dateTime', event['end'].get('date'))
                
                # Handle all-day events
                if isinstance(start, str) and len(start) == 10:  # YYYY-MM-DD
                    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
                    end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) - timedelta(microseconds=1)
                else:
                    start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
                    end_dt = datetime.fromisoformat(end.replace('Z', '+00:00'))
                
                formatted_events.append({
                    'id': event['id'],
                    'summary': event.get('summary', ''),
                    'date': start_dt.date().isoformat(),
                    'start_time': start_dt.time().strftime('%H:%M'),
                    'end_time': end_dt.time().strftime('%H:%M'),
                    'description': event.get('description', ''),
                    'location': event.get('location', '')
                })
            
            logger.info(f"✅ Fetched {len(formatted_events)} events")
            return formatted_events
            
        except HttpError as e:
            logger.error(f"❌ HTTP error fetching events: {e}")
            raise
        except Exception as e:
            logger.error(f"❌ Error fetching events: {e}")
            raise

    def check_availability(self, start_datetime: datetime, end_datetime: datetime) -> bool:
        """
        Check if time slot is available (no overlapping events).
        
        Args:
            start_datetime: Start time
            end_datetime: End time
            
        Returns:
            True if available, False if conflicting events exist
        """
        self._refresh_if_needed()
        
        try:
            events = self.service.events().list(
                calendarId='primary',
                timeMin=start_datetime.isoformat(),
                timeMax=end_datetime.isoformat(),
                singleEvents=True
            ).execute()
            
            conflicting = len(events.get('items', [])) > 0
            logger.info(f"✅ Availability check: {'available' if not conflicting else 'conflict'}")
            return not conflicting
            
        except HttpError as e:
            logger.error(f"❌ HTTP error checking availability: {e}")
            raise
        except Exception as e:
            logger.error(f"❌ Error checking availability: {e}")
            raise

    def create_event(
        self,
        summary: str,
        start_datetime: datetime,
        end_datetime: datetime,
        description: str = '',
        location: str = '',
        attendees: List[str] = None
    ) -> Dict:
        """
        Create a new calendar event.
        
        Args:
            summary: Event title
            start_datetime: Start time
            end_datetime: End time
            description: Event description
            location: Event location
            attendees: List of email addresses
            
        Returns:
            Created event dict from Google API
        """
        self._refresh_if_needed()
        
        try:
            event = {
                'summary': summary,
                'location': location,
                'description': description,
                'start': {
                    'dateTime': start_datetime.isoformat(),
                    'timeZone': 'UTC'
                },
                'end': {
                    'dateTime': end_datetime.isoformat(),
                    'timeZone': 'UTC'
                },
                'attendees': [{'email': email} for email in (attendees or [])],
                'reminders': {
                    'useDefault': True
                }
            }
            
            created_event = self.service.events().insert(
                calendarId='primary', 
                body=event,
                sendUpdates='all'
            ).execute()
            
            logger.info(f" Event created: {created_event.get('id')}")
            return created_event
            
        except HttpError as e:
            logger.error(f" HTTP error creating event: {e}")
            raise
        except Exception as e:
            logger.error(f" Error creating event: {e}")
            raise

    def get_updated_credentials(self) -> Dict:
        """
        Get current credentials (after potential refresh).
        
        Returns:
            Dict with access_token, refresh_token, token_expiry, scopes
        """
        return {
            'access_token': self.credentials.token,
            'refresh_token': self.credentials.refresh_token,
            'token_expiry': self.credentials.expiry,
            'scopes': self.credentials.scopes
        }