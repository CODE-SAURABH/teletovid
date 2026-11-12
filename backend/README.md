# Backend: Telegram topic to 9:16 explainer

The bot accepts a topic, runs a LangChain DeepAgent on Google Gemini, and returns
two files:

- `<slug>.html` - a self-contained 9:16 topic-directed explainer, 180-240 seconds
- `<slug>-script.json` - the storyboard script used to render the HTML

No database, TTS, MP4 rendering, or approval step is used. State is in memory and
resets when the process restarts.

## Files

- `bot.py` - Telegram commands, queue, and worker lifecycle
- `pipeline.py` - one-job worker, cancellation, retries, and file delivery
- `agent.py` - DeepAgent + Gemini runner and script extraction
- `prompts/agent.md` - editable agent system prompt
- `tools.py` - research searches, source-page excerpts, and script validation
- `theme.py` - deterministic HTML/CSS/JS renderer with topic layouts
- `health.py` - stdlib `/healthz` server for Render
- `config.py` / `state.py` - settings and in-memory jobs

## Environment

Copy `backend/.env.example` to `backend/.env` and fill:

```env
TELEGRAM_BOT_TOKEN=...
OWNER_TELEGRAM_ID=...
GOOGLE_API_KEY=...
AGENT_MODEL=gemini-3.6-flash
AGENT_TIMEOUT=600
AGENT_MAX_STEPS=24
AGENT_REQUESTS_PER_MINUTE=4
DAILY_JOB_LIMIT=3
SELF_PING_SECONDS=840
LOG_LEVEL=INFO
```

The agent's instructions live in `backend/prompts/agent.md`. Edit that file and
restart/redeploy to apply changes. The agent chooses a topic structure and a
layout for each scene; the renderer keeps the same self-contained visual theme
system while changing the composition and scene types.

Get the Gemini key from Google AI Studio. Free-tier limits apply. `AGENT_MODEL`
can be set to another free Gemini model, such as `gemini-3.5-flash-lite`.
`DAILY_JOB_LIMIT` defaults to 3 accepted jobs per UTC day.

DeepAgents requires Python 3.11 or newer. The local Windows virtual environment
in this repo is Python 3.10, so use Render's Python 3.12 runtime or a separate
3.12 environment for a real run.

## Run

From `backend`:

```bash
python bot.py
```

Or from the repository root:

```bash
python -m backend.bot
```

The agent is idle until a job is queued. Telegram polling and the health server
run in the same process.

## Commands

- `/add <topic>` - queue a 9:16 explainer
- `/now <topic>` - add at the front of the queue
- `/queue` - queue with Up / Down / Remove buttons
- `/remove <id>`, `/skip`, `/cancel <id>`, `/retry <id>`
- `/pause`, `/resume`
- `/status`, `/jobs`, `/profile`
- `/start`, `/ping`, `/help`

Only `OWNER_TELEGRAM_ID` receives responses.

## Render free tier

`render.yaml` defines a free Python Web Service:

- build: `pip install -r backend/requirements.txt`
- start: `python backend/bot.py`
- health check: `/healthz`
- region: Oregon
- Python: 3.12.8

Set `TELEGRAM_BOT_TOKEN`, `OWNER_TELEGRAM_ID`, and `GOOGLE_API_KEY` as secret
environment variables in Render. The free service sleeps after 15 minutes
without inbound traffic. The bot starts an internal keepalive thread that pings
its own `RENDER_EXTERNAL_URL/healthz` every 14 minutes (Render sets
`RENDER_EXTERNAL_URL` automatically). Set `SELF_PING_URL` to override the URL, or
`SELF_PING_SECONDS` to change the interval. Free instances have 512 MB RAM and no
free background worker, so a single process handles polling, the health endpoint,
and one generation job at a time.

## Limits

- One job at a time; queued jobs wait in memory.
- Three accepted jobs per UTC day by default; the counter resets at 00:00 UTC.
- Each job can use more than one Gemini request. If you hit free-tier 429s, lower
  `DAILY_JOB_LIMIT` or `AGENT_MAX_STEPS`.
- Restarting the service loses the queue, history, and daily counter.
- Free Gemini rate limits and Render memory can cause retries or failures.
- The HTML is rendered by the backend; the agent cannot write arbitrary HTML.
- Docker is optional and not used by Render.
