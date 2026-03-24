# GemGate — Self-hosted Google AI Gateway

> Zero API Key. Zero Cost. Just a free Google Account.

GemGate turns a single free Google account into a full multi-modal AI API gateway. Compatible with the OpenAI SDK — drop in your `base_url` and start building.

## Features

| Capability | Endpoint | Avg Response | Source |
|-----------|----------|-------------|--------|
| **Chat / LLM** | `POST /v1/chat/completions` | ~20s | Gemini |
| **Image Generation** | `POST /v1/images/generations` | ~45s | Gemini |
| **Text-to-Speech** | `POST /v1/audio/speech` | <1s | Google TTS |
| **Vision (Image Understanding)** | `POST /v1/chat/completions` (with image) | ~25s | Gemini |
| **Podcast Generation** | `POST /api/podcast/generate` | 6-16 min | NotebookLM |
| **Video Overview** | `POST /api/video/generate` | 4-12 min | NotebookLM |
| **Web Fetch** | `POST /api/web/fetch` | <1s | HTTP/RSS |
| **Model List** | `GET /v1/models` | instant | — |

### Additional Features

- **OpenAI SDK compatible** — Works with `openai` Python/JS SDK, Google Apps Script `UrlFetchApp`
- **Self-service API key registration** — Students visit landing page, enter name, get key
- **Per-key rate limiting** — RPM + daily quota per endpoint
- **Dual-account round-robin** — Load balance across 2 Google accounts
- **Admin dashboard API** — Monitor all keys and usage
- **Auto re-mux** — `ffmpeg` faststart fix for universal media playback

## Quick Start

### 1. Prerequisites

- Ubuntu 22.04+ (or any Linux with X11/Xvfb)
- Python 3.10+
- Firefox (installed by Playwright)
- ffmpeg
- A free Google account

### 2. Install

```bash
# Clone
git clone https://github.com/ai-cooperation/gemgate.git
cd gemgate

# Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Install Playwright Firefox
playwright install firefox

# Install ffmpeg
sudo apt-get install -y ffmpeg

# Virtual display (headless server)
sudo apt-get install -y xvfb x11vnc novnc
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your settings:
#   GEMGATE_PORT=8760
#   GEMGATE_ADMIN_SECRET=your-secret-here
#   GEMGATE_GOOGLE_EMAIL=your-google@gmail.com
#   GEMGATE_GOOGLE_PASS=your-password
```

### 4. Login Google Account

GemGate needs a logged-in Google session in Firefox. Use the login helper:

```bash
# Start virtual display
Xvfb :99 -screen 0 1280x900x24 -ac &
export DISPLAY=:99

# Start noVNC for remote access (optional)
x11vnc -display :99 -rfbauth ~/.vnc/passwd -forever -shared -bg
websockify --web=/usr/share/novnc/ --daemon 6080 localhost:5900

# Auto-login (or use noVNC to login manually)
python3 auto-login.py firefox-gemini https://gemini.google.com
python3 auto-login.py firefox-gemini-chat https://gemini.google.com
python3 auto-login.py firefox-notebooklm https://notebooklm.google.com
```

### 5. Run

```bash
export DISPLAY=:99
python3 main.py
# → GemGate running on http://0.0.0.0:8760
```

### 6. Systemd (Production)

```bash
sudo cp systemd/gemgate.service /etc/systemd/system/
sudo cp systemd/xvfb.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xvfb gemgate
```

## Usage

### Get an API Key

Visit `http://your-server:8760/` and enter your name to get a key (`gg-xxxx`).

### OpenAI SDK (Python)

```python
from openai import OpenAI

client = OpenAI(
    api_key="gg-your-key-here",
    base_url="http://your-server:8760/v1"
)

# Chat
resp = client.chat.completions.create(
    model="gemini",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(resp.choices[0].message.content)

# Image
img = client.images.generate(
    model="gemini-image",
    prompt="A sunset over mountains"
)
# img.data[0].b64_json contains the base64 PNG

# TTS
audio = client.audio.speech.create(
    model="gemini-tts",
    input="Hello world",
    voice="alloy"
)
# Returns MP3 binary
```

### Google Apps Script

```javascript
function askAI(prompt) {
  const url = "http://your-server:8760/v1/chat/completions";
  const options = {
    method: "post",
    headers: {
      "Authorization": "Bearer gg-your-key-here",
      "Content-Type": "application/json"
    },
    payload: JSON.stringify({
      model: "gemini",
      messages: [{ role: "user", content: prompt }]
    })
  };
  const res = UrlFetchApp.fetch(url, options);
  return JSON.parse(res.getContentText()).choices[0].message.content;
}
```

### cURL

```bash
# Chat
curl http://your-server:8760/v1/chat/completions \
  -H "Authorization: Bearer gg-your-key-here" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini","messages":[{"role":"user","content":"Hello!"}]}'

# Image
curl http://your-server:8760/v1/images/generations \
  -H "Authorization: Bearer gg-your-key-here" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-image","prompt":"A cute robot"}'

# TTS
curl http://your-server:8760/v1/audio/speech \
  -H "Authorization: Bearer gg-your-key-here" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-tts","input":"Hello world"}' \
  -o speech.mp3
```

### Podcast (Async)

Podcast and video generation are long-running tasks. Use the async job pattern:

```bash
# 1. Start generation (returns immediately)
curl -X POST http://your-server:8760/api/podcast/generate \
  -H "Content-Type: application/json" \
  -d '{
    "sources": [{"type": "text", "content": "Your content here..."}],
    "topic": "My Podcast Topic"
  }'
# → {"job_id": "abc123", "status": "pending", "poll_url": "/api/job/abc123"}

# 2. Poll for status (repeat every 30-60s)
curl http://your-server:8760/api/job/abc123
# → {"status": "generating", "estimated_seconds": 600, ...}

# 3. When complete (5-15 minutes)
curl http://your-server:8760/api/job/abc123
# → {"status": "completed", "output_base64": "...", "message": "Podcast ready (29.0MB)"}
```

Video generation follows the same pattern via `POST /api/video/generate`.

## API Reference

### OpenAI-Compatible Endpoints

| Method | Path | Auth | Description |
|--------|------|------|------------|
| GET | `/v1/models` | Bearer | List available models |
| POST | `/v1/chat/completions` | Bearer | Chat completion (supports Vision) |
| POST | `/v1/images/generations` | Bearer | Image generation |
| POST | `/v1/audio/speech` | Bearer | Text-to-Speech |
| GET | `/v1/usage` | Bearer | Your usage stats |

### GemGate Native Endpoints

| Method | Path | Auth | Description |
|--------|------|------|------------|
| GET | `/` | — | Landing page + registration |
| POST | `/register` | — | Self-service API key registration |
| GET | `/my/{key}` | — | Student usage dashboard |
| POST | `/api/podcast/generate` | — | Start podcast generation |
| POST | `/api/video/generate` | — | Start video generation |
| GET | `/api/job/{id}` | — | Poll async job status |
| GET | `/api/status` | — | System status |
| GET | `/admin/api/keys?secret=xxx` | Admin | List all API keys |

### Rate Limits (Default per Key)

| Endpoint | Daily Limit | RPM |
|----------|------------|-----|
| Chat | 50 | 5 |
| Image | 10 | 5 |
| TTS | 20 | 5 |
| Vision | 20 | 5 |
| Video | 3 | 5 |
| Podcast | 3 | 5 |

## Architecture

```
┌─────────────────────────────────────────────────┐
│                   GemGate                        │
│              FastAPI + Uvicorn                    │
├─────────────────────────────────────────────────┤
│  /v1/* (OpenAI Compat)  │  /api/* (Native)      │
│  ┌─────────────────┐    │  ┌─────────────────┐  │
│  │ API Key Auth     │    │  │ Job Queue       │  │
│  │ Rate Limiting    │    │  │ Quota Tracker   │  │
│  │ Round-Robin LB   │    │  │ Phase 2 Poller  │  │
│  └────────┬────────┘    │  └────────┬────────┘  │
├───────────┴─────────────┴───────────┴───────────┤
│              Firefox Manager                     │
│     (Playwright Persistent Context)              │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐ │
│  │ Gemini   │ │ Gemini   │ │ NotebookLM       │ │
│  │ Image    │ │ Chat     │ │ Podcast + Video   │ │
│  │ Profile  │ │ Profile  │ │ Profile           │ │
│  └──────────┘ └──────────┘ └──────────────────┘ │
├─────────────────────────────────────────────────┤
│  Xvfb (Virtual Display) + noVNC (Remote Login)  │
└─────────────────────────────────────────────────┘
```

### Key Design Decisions

- **Playwright Firefox persistent context** — Google session persists across restarts without cookie injection
- **Per-profile job queue** — Tasks sharing a Firefox profile are serialized to prevent navigation conflicts
- **Phase 1 + Phase 2 pattern** — Long-running NotebookLM tasks: Phase 1 starts generation (~60s), Phase 2 polls and downloads (5-15 min)
- **ffmpeg re-mux** — NotebookLM outputs DASH format; `ffmpeg -c copy -movflags +faststart` fixes playback in all players
- **Dual-account round-robin** — Distributes load across 2 Google accounts to reduce rate-limit risk

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMGATE_PORT` | `8760` | Server port |
| `GEMGATE_BIND` | `0.0.0.0` | Bind address |
| `GEMGATE_API_KEY` | _(empty)_ | Global API key for legacy `/api/*` endpoints |
| `GEMGATE_ADMIN_SECRET` | _(required)_ | Admin dashboard secret |
| `GEMGATE_HEADLESS` | `false` | Run Firefox headless (no Xvfb needed) |
| `DISPLAY` | `:99` | X11 display for Firefox |

### Dual Account Setup

Edit `config.py` to add a second Google account:

```python
DUAL_ACCOUNT_PROFILES = {
    "gemini_image": ["firefox-gemini", "firefox-gemini-2"],
    "gemini_chat": ["firefox-gemini-chat", "firefox-gemini-chat-2"],
}
```

Then login the second account:
```bash
python3 auto-login.py firefox-gemini-2 https://gemini.google.com
python3 auto-login.py firefox-gemini-chat-2 https://gemini.google.com
```

## Deployment

### Recommended: Tencent Cloud / AWS Lightsail

- **Spec**: 2 vCPU / 4 GB RAM / 60 GB SSD
- **Cost**: ~$3-6/month
- **Region**: US West (close to Google servers)
- RAM usage: ~1 GB idle, ~2.1 GB with 3 Firefox instances

### Security Notes

- GemGate **never stores** Google passwords in code or config
- Google sessions are stored in Firefox profile directories (persistent context)
- noVNC is used only for initial Google login — restrict access or disable after setup
- API keys are hashed and stored in SQLite
- Admin endpoints require a separate secret

## Project Structure

```
gemgate/
├── main.py                    # FastAPI app entry point
├── config.py                  # Configuration
├── auto-login.py              # Google account auto-login helper
├── requirements.txt           # Python dependencies
├── .env.example               # Environment template
├── core/
│   ├── api_keys.py            # API key management + rate limiting
│   ├── auth.py                # Authentication middleware
│   ├── firefox_manager.py     # Firefox lifecycle management
│   ├── queue.py               # Per-profile job queue
│   ├── quota.py               # Daily quota tracking (SQLite)
│   └── models.py              # Pydantic models
├── routers/
│   ├── openai_compat.py       # /v1/* OpenAI-compatible endpoints
│   ├── register.py            # Landing page + registration
│   ├── podcast.py             # Podcast generation (Phase 1 + 2)
│   ├── video.py               # Video generation (Phase 1 + 2)
│   ├── image.py               # Image generation
│   ├── llm.py                 # LLM chat
│   ├── tts.py                 # Text-to-Speech
│   ├── vision.py              # Vision analysis
│   ├── web.py                 # Web fetch
│   └── admin.py               # Admin + job status
├── providers/
│   ├── base.py                # Base provider class
│   ├── gemini_image_ff.py     # Gemini image (Firefox)
│   ├── gemini_chat_ff.py      # Gemini chat (Firefox)
│   ├── gemini_video_ff.py     # Gemini video/Veo (Firefox)
│   ├── gemini_audio_ff.py     # Gemini audio/music (Firefox)
│   ├── notebooklm_ff.py       # NotebookLM podcast (Firefox)
│   ├── notebooklm_video_ff.py # NotebookLM video (Firefox)
│   ├── flow_image_ff.py       # Google Flow image (Firefox)
│   ├── google_tts.py          # Google TTS (gTTS library)
│   └── web_fetcher_ff.py      # Web content fetcher
└── systemd/
    ├── gemgate.service         # GemGate systemd unit
    └── xvfb.service            # Virtual display systemd unit
```

## Comparison

| Feature | GemGate | Project Golem | LiteLLM |
|---------|---------|--------------|---------|
| API Key Cost | **$0** | $0 | Requires API keys |
| Chat | ✅ | ✅ | ✅ |
| Image Generation | ✅ | ✅ | ✅ |
| Video Generation | ✅ | ❌ | ❌ |
| Podcast | ✅ | ❌ | ❌ |
| TTS | ✅ | ❌ | ✅ |
| Vision | ✅ | ❌ | ✅ |
| OpenAI SDK Compatible | ✅ | ❌ | ✅ |
| Multi-user Key Management | ✅ | ❌ | ✅ |
| Self-hosted | ✅ | ✅ | ✅ |
| Shell Execution | ❌ (API only) | ✅ (Agent) | ❌ |

## License

MIT

## Credits

Built by [AI Cooperation](https://github.com/ai-cooperation) for the AI 100 Lectures teaching program.
