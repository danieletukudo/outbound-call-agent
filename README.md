# LiveKit Outbound Sales Voice Agent

Outbound phone agent built on **LiveKit Agents** + **Twilio Elastic SIP**. One main file (`voice_agent.py`) is both:

1. The **worker** that joins calls and talks (STT → LLM → TTS)
2. The **dialer** that creates a LiveKit room and dispatches the agent to call a number

---

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- A [LiveKit Cloud](https://cloud.livekit.io) project
- A Twilio account with an **Elastic SIP Trunk** registered as a LiveKit outbound SIP trunk
- Geographic permissions in Twilio for the countries you dial

---

## Setup

```bash
cd livekit-agent
uv sync
cp .env.example .env
```

Edit `.env` with your LiveKit credentials and SIP trunk ID (see [Environment variables](#environment-variables)).

---

## How to use

Always run commands from the `livekit-agent` directory. Prefer activating this project’s venv so `uv` doesn’t warn about a mismatched `VIRTUAL_ENV`:

```bash
source .venv/bin/activate
```

### 1. Start the worker (required for phone calls)

Keep this running in **terminal A**:

```bash
uv run python voice_agent.py start
```

You should see logs like `registered worker` with `agent_name: my-agent`.

### 2. Place an outbound call

In **terminal B**:

```bash
uv run python voice_agent.py call +15551234567
uv run python voice_agent.py call +15551234567 --name Alex
```

Numbers must be **E.164** (start with `+`, country code, no spaces).

### 3. Local mic test (no phone)

```bash
uv run python voice_agent.py console
```

Uses your microphone/speakers instead of SIP.

### Other LiveKit CLI modes

Anything that is not `call` is passed to LiveKit’s agent CLI, e.g.:

```bash
uv run python voice_agent.py start
uv run python voice_agent.py console
uv run python voice_agent.py --help
```

---

## Call flow (what happens)

```
Terminal B: voice_agent.py call +1...
        │
        ▼
Creates LiveKit room + AgentDispatch
(metadata: phone_number, optional contact_name)
        │
        ▼
Terminal A: worker receives job → entrypoint()
        │
        ▼
Starts AgentSession (VAD / STT / LLM / TTS)
        │
        ▼
CreateSIPParticipant (Twilio via LiveKit trunk)
        │
        ▼
Callee answers → session.say(opening)  [TTS only, no LLM]
        │
        ▼
Conversation loop (Flux STT turn detection → Gemma → Cartesia)
        │
        ▼
end_call tool → delete room → hang up
```

---

## Project layout

| Path | Purpose |
|------|---------|
| `voice_agent.py` | Agent + dialer (main app) |
| `.env` / `.env.example` | Config and secrets |
| `pyproject.toml` / `uv.lock` | Dependencies |
| `Dockerfile` | Container image for LiveKit Cloud deploy |
| `livekit.toml` | LiveKit Cloud agent project config |
| `deploy-livekit-cloud.sh` | Helper script to push env / deploy |

---

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `LIVEKIT_URL` | yes | e.g. `wss://your-project.livekit.cloud` |
| `LIVEKIT_API_KEY` | yes | API key |
| `LIVEKIT_API_SECRET` | yes | API secret |
| `AGENT_NAME` | no | Dispatch name; default `my-agent` (must match worker + dialer) |
| `SIP_OUTBOUND_TRUNK_ID` | yes (calls) | LiveKit outbound trunk ID (`ST_...`) |
| `TWILIO_FROM_NUMBER` | recommended | Caller ID in E.164 |
| `LK_STT_MODEL` | no | Default `deepgram/flux-general` |
| `LK_LLM_MODEL` | no | Default `google/gemma-4-31b-it` |
| `LK_TTS_MODEL` | no | Default `cartesia/sonic-turbo` |
| `LK_TTS_VOICE` | no | Cartesia voice UUID |
| `LK_TTS_SPEED` | no | `slow` / `normal` / `fast` or a float |
| `COMPANY_NAME` | no | Used in greeting + instructions |
| `COMPANY_PITCH` | no | Short value pitch in instructions |
| `NUM_IDLE_PROCESSES` | no | Warm worker processes; default `1` |

Models are served via **LiveKit Inference** (no separate OpenAI/Deepgram/Cartesia API keys required for the defaults).

---

## Latency notes

This agent is tuned for snappy phone conversation:

- **Opening line** uses `session.say()` (TTS only) so speech starts right after pickup without waiting for the LLM.
- **Turn detection** uses Deepgram Flux (`turn_detection: "stt"`) instead of a separate TurnDetector model.
- **Endpointing** waits at most ~0.4s after the user stops.
- **Preemptive TTS** starts synthesizing before the turn is fully confirmed.
- Prefer `LK_TTS_SPEED=normal`; `fast` can sound choppy.

---

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| Call dispatches but nothing rings | Worker not running (`voice_agent.py start`) |
| SIP `408` timeout | No answer, Twilio geo permissions, or trunk misconfig |
| `VIRTUAL_ENV ... will be ignored` | Parent venv active; run `source .venv/bin/activate` in this folder |
| Agent never speaks after answer | Check worker logs for dial / TTS / model errors |
| Hangup fails | Fixed via `RunContext.wait_for_playout()` in `end_call` |

---

## Code reference: `voice_agent.py` (line by line)

### Module docstring (lines 1–13)

| Line(s) | What it does |
|---------|----------------|
| 1–13 | Documents how to run the worker, place a call, and use console mode. |

### Imports and env bootstrap (lines 15–43)

| Line(s) | What it does |
|---------|----------------|
| 15 | `from __future__ import annotations` — postpones evaluation of type hints (cleaner forward refs). |
| 17 | `argparse` — parses `call` CLI arguments. |
| 18 | `asyncio` — runs the async dialer with `asyncio.run(...)`. |
| 19 | `json` — encodes/decodes job metadata (`phone_number`, `contact_name`). |
| 20 | `logging` — structured agent logs. |
| 21 | `os` — reads environment variables. |
| 22 | `re` — E.164 phone validation regex. |
| 23 | `sys` — CLI argv + stderr exit for dialer errors. |
| 24 | `uuid` — unique outbound room names. |
| 25 | `Any` typing for metadata dicts. |
| 27–29 | Loads `.env` into the process environment before LiveKit imports need credentials. |
| 31 | `livekit.api` — CreateSIPParticipant, CreateAgentDispatch, DeleteRoom. |
| 32–43 | LiveKit Agents building blocks: `Agent`, `AgentServer`, `AgentSession`, job/run context, CLI, tools, Inference STT/LLM/TTS, room I/O. `# noqa: E402` allows imports after `load_dotenv()`. |

### Logger and config (lines 45–75)

| Line(s) | What it does |
|---------|----------------|
| 45 | Named logger `voice-agent` for dial / greeting logs. |
| 47 | `AGENT_NAME` — must match between worker registration and dispatch. |
| 48 | How many idle worker processes to keep warm. |
| 51 | STT model (Deepgram Flux via LiveKit Inference). |
| 52 | LLM model (Gemma 4 31B — latency-oriented default). |
| 53 | TTS model (Cartesia Sonic Turbo). |
| 54–58 | Cartesia voice ID. |
| 59–60 | Speaking rate; default `normal` for natural phone audio. |
| 61–65 | Company name + pitch injected into prompts. |
| 66–67 | Outbound SIP trunk ID and Twilio caller ID (stripped of whitespace). |
| 69 | Regex: `+` then country code digit 1–9, then 7–14 more digits. |
| 71–75 | Fixed opening sentence spoken with TTS only after answer. |

### Agent instructions (lines 77–94)

| Line(s) | What it does |
|---------|----------------|
| 77–94 | System prompt for the LLM: short replies, no filler, light sales goals, hang up on decline/voicemail via `end_call`. |

### `_parse_dial_info` (lines 97–108)

| Line(s) | What it does |
|---------|----------------|
| 97 | Parses job `metadata` from dispatch into a dict. |
| 98–99 | Empty metadata → `{}` (console / no dial). |
| 100–101 | Prefer JSON object from dialer. |
| 102–103 | Non-JSON string → treat whole string as phone number. |
| 104–105 | JSON string value → wrap as `phone_number`. |
| 106–107 | JSON object → return as-is. |
| 108 | Anything else → empty dict. |

### `SalesOutreachAgent` (lines 111–131)

| Line(s) | What it does |
|---------|----------------|
| 111 | Agent subclass with instructions + tools. |
| 112–114 | `greet_on_enter`: only auto-greet in console (no phone); outbound greets after SIP answer. |
| 114 | Passes `AGENT_INSTRUCTIONS` to the base `Agent`. |
| 116 | `@function_tool` exposes `end_call` to the LLM. |
| 117–118 | Tool entry; docstring is shown to the model as tool description. |
| 119 | Wait for current speech to finish **via RunContext** (avoids circular wait with SpeechHandle). |
| 120 | Get the active job (room + API client). |
| 121–124 | Delete the LiveKit room → tears down SIP and ends the call. |
| 125 | Tool return string for the LLM. |
| 127–129 | `on_enter`: if not greeting on enter, do nothing. |
| 130–131 | Console mode: speak opening line with TTS only. |

### Worker server (lines 134–137)

| Line(s) | What it does |
|---------|----------------|
| 134–137 | `AgentServer` hosts jobs; 60s process init timeout; `num_idle_processes` warm pool. |

### `entrypoint` (lines 140–232)

| Line(s) | What it does |
|---------|----------------|
| 140 | Registers this coroutine as the RTC session handler for `AGENT_NAME`. |
| 141 | Job entry when LiveKit assigns a room to this worker. |
| 142 | Attach room name to every log line. |
| 144–146 | Read phone + optional contact name from dispatch metadata. |
| 148 | Outbound (`phone_number` set) → don’t greet in `on_enter`; console → greet immediately. |
| 150–154 | Allow `LK_TTS_SPEED` as float (`1.1`) or label (`normal`). |
| 156–181 | Build the realtime pipeline: |
| 158 | VAD — detects user speech for barge-in / interruptions. |
| 159–168 | Flux STT with eager end-of-turn thresholds for faster replies. |
| 169 | LLM for dialogue after the fixed opening. |
| 170–174 | TTS voice + speed. |
| 175–180 | Turn handling: STT-based turns, short endpointing, preemptive TTS, quick interrupt. |
| 183–187 | Start the session attached to the room. |
| 188 | Connect the worker participant to the room. |
| 190–192 | No phone → console session; skip SIP. |
| 194–195 | Fail fast if trunk ID missing. |
| 197–204 | Log dial target / trunk / caller ID / LLM. |
| 206–214 | Build SIP dial request; wait until answered; play dial tone to the room. |
| 215–216 | Set Twilio From number when configured. |
| 217 | Place the outbound SIP call via LiveKit. |
| 218–219 | Wait until the callee participant appears in the room. |
| 220–223 | On dial failure: log, shut down job, exit. |
| 225–232 | After answer: personalized opening if `--name` was passed; `session.say()` starts TTS immediately (no LLM lag). |

### `dispatch_call` (lines 235–277)

| Line(s) | What it does |
|---------|----------------|
| 235–236 | Dialer entry used by `voice_agent.py call`. |
| 237–244 | Require LiveKit URL/key/secret. |
| 246–248 | Require SIP trunk. |
| 250–255 | Validate E.164. |
| 257 | Unique room name `outbound-<10 hex chars>`. |
| 258–260 | Metadata payload for the worker. |
| 262–269 | Create an agent dispatch; LiveKit routes the job to a registered worker named `AGENT_NAME`. |
| 271–277 | Print room / number / trunk / dispatch id for debugging. |

### `main` and entry (lines 280–297)

| Line(s) | What it does |
|---------|----------------|
| 280–281 | CLI switch: custom `call` vs LiveKit `cli.run_app`. |
| 282–291 | Parse `phone_number` and optional `--name`, then run `dispatch_call`. |
| 293 | `start` / `console` / other LiveKit agent commands. |
| 296–297 | Standard Python entrypoint guard. |

---

## Deploy to LiveKit Cloud (optional)

Use the included Docker + `livekit.toml` / `deploy-livekit-cloud.sh` if you want the worker hosted on LiveKit Cloud instead of your laptop. Local `voice_agent.py start` is enough for development and testing calls.
# outbound-call-agent
