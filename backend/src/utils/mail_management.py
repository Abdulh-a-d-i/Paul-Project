import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
import pytz

class Send_Mail:
    def __init__(self):
        self.MAIL_SENDER = "imabdul.hadi1234@gmail.com"
        self.EMAIL_PASSWORD = "gqty jnji ufjq rgrh"
        self.TIMEZONE = "America/New_York"

    async def send_email_with_calendar_event(
        self,
        attendee_email: str,
        attendee_name: str,
        appointment_date: str,
        start_time: str,
        end_time: str,
        title: str,
        description: str,
        organizer_name: str,
        organizer_email: str,
    ):
        try:
            tz = pytz.timezone(self.TIMEZONE)
            start_dt = tz.localize(datetime.strptime(f"{appointment_date} {start_time}", "%Y-%m-%d %H:%M"))
            end_dt = tz.localize(datetime.strptime(f"{appointment_date} {end_time}", "%Y-%m-%d %H:%M"))

            dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
            dtstart = start_dt.astimezone(pytz.UTC).strftime("%Y%m%dT%H%M%SZ")
            dtend = end_dt.astimezone(pytz.UTC).strftime("%Y%m%dT%H%M%SZ")
            uid = f"{dtstamp}@{organizer_email.split('@')[1]}"

            # ✅ Proper RFC-compliant ICS
            ics_content = f"""BEGIN:VCALENDAR
PRODID:-//YourCompany//AI Scheduler//EN
VERSION:2.0
CALSCALE:GREGORIAN
METHOD:REQUEST
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{dtstamp}
DTSTART:{dtstart}
DTEND:{dtend}
SUMMARY:{title}
DESCRIPTION:{description}
LOCATION:Online Meeting
STATUS:CONFIRMED
ORGANIZER;CN={organizer_name}:MAILTO:{organizer_email}
ATTENDEE;CN={attendee_name};RSVP=TRUE:MAILTO:{attendee_email}
BEGIN:VALARM
TRIGGER:-PT15M
ACTION:DISPLAY
DESCRIPTION:Reminder
END:VALARM
END:VEVENT
END:VCALENDAR
""".replace("\n", "\r\n")

            # ✅ Correct multipart/alternative email
            msg = MIMEMultipart("mixed")
            msg["Subject"] = f"Appointment Confirmation: {title}"
            msg["From"] = f"{organizer_name} <{organizer_email}>"
            msg["To"] = attendee_email

            alternative = MIMEMultipart("alternative")
            msg.attach(alternative)

            # Email body
            plain_body = (
                f"Dear {attendee_name},\n\n"
                f"Your appointment has been scheduled.\n\n"
                f"📅 Date: {appointment_date}\n"
                f"🕒 Time: {start_time} - {end_time}\n"
                f"📝 Notes: {description or 'N/A'}\n\n"
                f"Best regards,\n{organizer_name}"
            )

            html_body = f"""
            <html>
                <body>
                    <p>Dear {attendee_name},</p>
                    <p>Your appointment has been scheduled.</p>
                    <ul>
                        <li><b>Date:</b> {appointment_date}</li>
                        <li><b>Time:</b> {start_time} - {end_time}</li>
                        <li><b>Notes:</b> {description or 'N/A'}</li>
                    </ul>
                    <p>You can accept or decline the meeting using your calendar buttons.</p>
                    <p>Best regards,<br>{organizer_name}</p>
                </body>
            </html>
            """

            alternative.attach(MIMEText(plain_body, "plain"))
            alternative.attach(MIMEText(html_body, "html"))

            # ✅ Attach ICS as proper calendar part
            ics_part = MIMEBase("text", "calendar", method="REQUEST", name="invite.ics")
            ics_part.set_payload(ics_content)
            encoders.encode_base64(ics_part)
            ics_part.add_header("Content-Transfer-Encoding", "base64")
            ics_part.add_header("Content-Disposition", "attachment; filename=invite.ics")
            ics_part.add_header("Content-Class", "urn:content-classes:calendarmessage")

            msg.attach(ics_part)

            # ✅ Send email
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self.MAIL_SENDER, self.EMAIL_PASSWORD)
                server.send_message(msg)

            logging.info(f"✅ Email with calendar invite sent to {attendee_email}")
            return True

        except Exception as e:
            logging.error(f"❌ Error sending email with calendar event: {e}")
            return False
