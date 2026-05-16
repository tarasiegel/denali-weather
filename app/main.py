"""
Denali Weather Report Bot
-------------------------
FastAPI webhook server that:
1. Receives Twilio recording callbacks
2. Transcribes audio via OpenAI Whisper
3. Delivers the transcription to a Garmin inReach via email gateway
"""

import os
import smtplib
import tempfile
from datetime import datetime
from email.mime.text import MIMEText

import httpx
import openai
from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse
from twilio.rest import Client
from twilio.twiml.voice_response import Pause, VoiceResponse

app = FastAPI(title="Denali Weather Bot")

# ---------------------------------------------------------------------------
# Config — set these in your environment or .env file
# ---------------------------------------------------------------------------
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_FROM_NUMBER = os.environ["TWILIO_FROM_NUMBER"]   # your Twilio number
NPS_HOTLINE_NUMBER = os.environ["NPS_HOTLINE_NUMBER"]   # the NPS hotline number
NPS_EXTENSION      = os.environ.get("NPS_EXTENSION", "1")  # DTMF extension digits

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# Garmin inReach email gateway
# Format: <15-digit-imei>@inreach.garmin.com  (no country code, just the IMEI)
INREACH_EMAIL = os.environ["INREACH_EMAIL"]

# SMTP config — Gmail shown here; works with any SMTP provider
SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ["SMTP_USER"]      # your Gmail address
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]  # Gmail App Password (not your login password)

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)

# Public base URL of this server — Twilio needs to reach it
# e.g. https://your-app.onrender.com  or your ngrok URL during dev
PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/")
def health():
    return {"status": "ok", "service": "denali-weather-bot"}


# ---------------------------------------------------------------------------
# Step 1: Place the outbound call (called by the daily scheduler)
# ---------------------------------------------------------------------------
@app.post("/trigger-call")
def trigger_call():
    """
    Initiates the outbound call to the NPS hotline.
    The TwiML at /twiml-instructions tells Twilio what to do on the call.
    """
    call = twilio_client.calls.create(
        to=NPS_HOTLINE_NUMBER,
        from_=TWILIO_FROM_NUMBER,
        url=f"{PUBLIC_BASE_URL}/twiml-instructions",
        status_callback=f"{PUBLIC_BASE_URL}/call-status",
        status_callback_event=["completed"],
    )
    print(f"[{datetime.utcnow().isoformat()}] Call initiated — SID: {call.sid}")
    return {"call_sid": call.sid}


# ---------------------------------------------------------------------------
# Step 2: TwiML instructions — what to do during the call
# ---------------------------------------------------------------------------
@app.post("/twiml-instructions", response_class=PlainTextResponse)
def twiml_instructions():
    """
    Returns TwiML XML that Twilio executes during the call:
    - Pause to let the greeting play
    - Send DTMF digits for the extension
    - Pause again while the weather report starts
    - Record up to 3 minutes of audio
    """
    response = VoiceResponse()

    # Wait for the automated greeting
    response.append(Pause(length=6))

    # Dial the extension
    response.play(digits=NPS_EXTENSION)

    # Wait for the report to begin
    response.append(Pause(length=10))
    
    # Record — Twilio will POST the recording URL to /handle-recording
    response.record(
        action=f"{PUBLIC_BASE_URL}/handle-recording",
        max_length=300,          #5 minutes max
        timeout=10,               # stop after 10s of silence
        recording_status_callback=f"{PUBLIC_BASE_URL}/handle-recording",
        recording_status_callback_event=["completed"],
        play_beep=False,
    )

    return str(response)


# ---------------------------------------------------------------------------
# Step 3: Receive the completed recording and transcribe + SMS it
# ---------------------------------------------------------------------------
@app.post("/handle-recording")
async def handle_recording(
    RecordingUrl: str = Form(None),
    RecordingStatus: str = Form(None),
    RecordingSid: str = Form(None),
):
    """
    Twilio POSTs here when a recording is ready.
    We download the audio, transcribe it, then deliver it to the inReach device.
    """
    if RecordingStatus and RecordingStatus != "completed":
        return {"ignored": True, "status": RecordingStatus}

    if not RecordingUrl:
        print("No RecordingUrl received")
        return {"error": "no recording url"}

    # Twilio appends .mp3 for the actual audio file
    audio_url = RecordingUrl + ".mp3"
    print(f"Downloading recording: {audio_url}")

    # Download the recording (Twilio requires auth)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            audio_url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            follow_redirects=True,
            timeout=60,
        )
        resp.raise_for_status()
        audio_bytes = resp.content

    # Write to a temp file for Whisper
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    print(f"Transcribing {len(audio_bytes) // 1024} KB of audio...")

    with open(tmp_path, "rb") as audio_file:
        transcription = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            # Prompt helps Whisper handle NPS/weather vocabulary
            prompt=(
                "This is a National Park Service weather report for Denali National Park. "
                "It may include wind speeds, temperatures in Fahrenheit, visibility, "
                "precipitation, and elevation references."
            ),
        )

    transcript_text = transcription.text.strip()
    print(f"Transcription:\n{transcript_text}")

    # Format and deliver to inReach
    today = datetime.utcnow().strftime("%b %d, %Y")
    # _send_to_inreach(transcript_text, today)
    _send_to_email(transcript_text, today)

    # Clean up temp file
    os.unlink(tmp_path)

    return {"status": "sent", "chars": len(transcript_text)}


# inReach has a 160-character message limit per message.
INREACH_MSG_LIMIT = 155  # leave a few chars of headroom
def _send_to_inreach(transcript: str, date_str: str):
    """
    Sends the weather report to a Garmin inReach device via the
    @inreach.garmin.com email gateway.

    inReach caps incoming messages at 160 characters, so we split the
    transcript into chunks and send each as a separate email.
    The subject line is ignored by the gateway; only the body is delivered.
    """
    # Build chunks that fit within the inReach character limit
    words = transcript.split()
    chunks = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= INREACH_MSG_LIMIT:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = word
    if current:
        chunks.append(current)

    total = len(chunks)
    print(f"Sending {total} message(s) to inReach: {INREACH_EMAIL}")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)

        for i, chunk in enumerate(chunks, start=1):
            # Prefix first chunk with a short header; subsequent chunks are plain text
            if i == 1 and total == 1:
                body = f"Denali {date_str}: {chunk}"
            elif i == 1:
                body = f"Denali {date_str} (1/{total}): {chunk}"
            else:
                body = f"({i}/{total}): {chunk}"

            msg = MIMEText(body)
            msg["Subject"] = f"Denali Weather {date_str}"
            msg["From"]    = SMTP_USER
            msg["To"]      = INREACH_EMAIL

            server.sendmail(SMTP_USER, INREACH_EMAIL, msg.as_string())
            print(f"  Sent message {i}/{total}: {body[:60]}...")

def _send_to_email(message: str, date_str: str):
    """
    Sends the weather report to an email address.

    Args:
        message: The weather report message.
        date_str: The date string.
    """
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        msg = MIMEText(message)
        msg["Subject"] = f"Denali Weather {date_str}"
        msg["From"]    = SMTP_USER
        msg["To"]      = INREACH_EMAIL
        server.sendmail(SMTP_USER, INREACH_EMAIL, msg.as_string())
        print(f"  Sent message: {message[:60]}...")


# ---------------------------------------------------------------------------
# Optional: call status callback (for logging/debugging)
# ---------------------------------------------------------------------------
@app.post("/call-status")
async def call_status(request: Request):
    form = await request.form()
    print(f"Call status update: {dict(form)}")
    return {"ok": True}
