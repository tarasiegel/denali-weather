# 🏔 Denali Weather Bot

Calls the NPS Denali weather hotline daily, transcribes the report via Whisper, and texts it to your phone.

## How It Works

```
GitHub Actions (daily cron)
    → POST /trigger-call
    → Twilio places outbound call to NPS hotline
    → TwiML: pause → press extension → record
    → Twilio POSTs recording URL to /handle-recording
    → Whisper transcribes the audio
    → Twilio SMS sends transcript to your phone
```

## Setup

### 1. Clone & install

```bash
git clone <your-repo>
cd denali-weather
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your real values
```

You need accounts with:
- [Twilio](https://twilio.com) — for calls and SMS (~$0.05/day)
- [OpenAI](https://platform.openai.com) — for Whisper transcription (~$0.001/day)

### 3. Run locally for development

```bash
# Terminal 1 — start the server
uvicorn app.main:app --reload --port 8000

# Terminal 2 — expose it publicly so Twilio can reach you
ngrok http 8000
# Copy the https://xxxxx.ngrok.io URL into your .env as PUBLIC_BASE_URL
```

### 4. Test the call flow manually

```bash
# With your server running and PUBLIC_BASE_URL set:
curl -X POST http://localhost:8000/trigger-call
```

Watch your terminal — you should see:
- Call initiated log
- Twilio calling the hotline
- Recording URL received
- Transcription printed
- SMS sent confirmation

### 5. Deploy to production (Render)

1. Push to GitHub
2. Create a new Web Service on [Render](https://render.com)
3. Connect your repo — Render detects `render.yaml` automatically
4. Add all environment variables in the Render dashboard
5. Copy your Render URL into `PUBLIC_BASE_URL`

### 6. Set up the daily schedule (GitHub Actions)

1. In your repo → Settings → Secrets → Actions, add `PUBLIC_BASE_URL`
2. The workflow in `.github/workflows/daily-call.yml` runs at 8am AKDT
3. You can also trigger it manually from the Actions tab

## Tuning the IVR Timing

If the bot isn't capturing the report correctly, adjust the `Pause(length=...)` values in `twiml_instructions()`:

```python
response.append(Pause(length=6))   # ← increase if greeting is long
response.play(digits=NPS_EXTENSION)
response.append(Pause(length=3))   # ← increase if report is slow to start
response.record(timeout=5, ...)    # ← seconds of silence before stopping
```

Call the hotline manually first and time how long the greeting takes.

## File Structure

```
denali-weather/
├── app/
│   └── main.py              # FastAPI app (the brain)
├── scripts/
│   └── trigger_daily.py     # Standalone trigger script (for cron)
├── .github/
│   └── workflows/
│       └── daily-call.yml   # GitHub Actions scheduler
├── .env.example             # Environment variable template
├── render.yaml              # One-click Render deployment
├── requirements.txt
└── README.md
```

## Cost Estimate

| Service | Usage | Est. Cost/Day |
|---|---|---|
| Twilio outbound call | ~2 min | ~$0.02 |
| Twilio SMS | 1 message | ~$0.008 |
| OpenAI Whisper | ~2 min audio | ~$0.012 |
| Render hosting | Free tier | $0 |
| **Total** | | **~$0.04/day** |
