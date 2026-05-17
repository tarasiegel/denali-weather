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

# SendGrid — used for email delivery via HTTPS (works on Render free tier)
# 1. Sign up free at sendgrid.com (100 emails/day free)
# 2. Settings -> API Keys -> Create API Key (Full Access)
# 3. Settings -> Sender Authentication -> verify your From email address
SENDGRID_API_KEY    = os.environ["SENDGRID_API_KEY"]
SENDGRID_FROM_EMAIL = os.environ["SENDGRID_FROM_EMAIL"]  # must be verified in SendGrid

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
    await _send_to_email(transcript_text, today)

    # Clean up temp file
    os.unlink(tmp_path)

    return {"status": "sent", "chars": len(transcript_text)}


# inReach has a 160-character message limit per message.
INREACH_MSG_LIMIT = 155  # leave a few chars of headroom
SENDGRID_API_URL  = "https://api.sendgrid.com/v3/mail/send"

async def _send_to_inreach(transcript: str, date_str: str):
    """
    Sends the weather report to a Garmin inReach via SendGrid's HTTP API.
    Splits into word-boundary chunks to fit the 160-char inReach limit.
    """
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
 
    headers = {
        "Authorization": f"Bearer {SENDGRID_API_KEY}",
        "Content-Type": "application/json",
    }
 
    async with httpx.AsyncClient() as client:
        for i, chunk in enumerate(chunks, start=1):
            if i == 1 and total == 1:
                body = f"Denali {date_str}: {chunk}"
            elif i == 1:
                body = f"Denali {date_str} (1/{total}): {chunk}"
            else:
                body = f"({i}/{total}): {chunk}"
 
            payload = {
                "personalizations": [{"to": [{"email": INREACH_EMAIL}]}],
                "from": {"email": SENDGRID_FROM_EMAIL},
                "subject": f"Denali Weather {date_str}",
                "content": [{"type": "text/plain", "value": body}],
            }
 
            resp = await client.post(
                SENDGRID_API_URL,
                headers=headers,
                json=payload,
                timeout=15,
            )
            resp.raise_for_status()
            print(f"  Sent message {i}/{total}: {body[:60]}...")


async def _send_to_email(transcript: str, date_str: str):
    """
    Sends the full weather report as a single email (no chunking).
    Useful for regular email inboxes, not subject to the inReach 160-char limit.
    """
 
    body = f"Denali National Park Weather Report — {date_str} \n\n {transcript}"
 
    headers = {
        "Authorization": f"Bearer {SENDGRID_API_KEY}",
        "Content-Type": "application/json",
    }
 
    payload = {
        "personalizations": [{"to": [{"email": INREACH_EMAIL}]}],
        "from": {"email": SENDGRID_FROM_EMAIL},
        "subject": f"Denali Weather Report — {date_str}",
        "content": [{"type": "text/plain", "value": body}],
    }
 
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            SENDGRID_API_URL,
            headers=headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        print(f"Full report emailed to {INREACH_EMAIL}")


# ---------------------------------------------------------------------------
# Optional: call status callback (for logging/debugging)
# ---------------------------------------------------------------------------
@app.post("/call-status")
async def call_status(request: Request):
    form = await request.form()
    print(f"Call status update: {dict(form)}")
    return {"ok": True}
