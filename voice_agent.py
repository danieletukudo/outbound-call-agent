"""
LiveKit outbound sales agent + dialer (one file).

Worker (keep running):
  uv run python voice_agent.py start

Place a call (another terminal):
  uv run python voice_agent.py call +15551234567
  uv run python voice_agent.py call +15551234567 --name Alex

Local mic test:
  uv run python voice_agent.py console
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import uuid
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from livekit import api  # noqa: E402
from livekit.agents import (  # noqa: E402
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    RunContext,
    cli,
    function_tool,
    get_job_context,
    inference,
    room_io,
)

logger = logging.getLogger("voice-agent")

AGENT_NAME = os.getenv("AGENT_NAME", "my-agent")
NUM_IDLE_PROCESSES = int(os.getenv("NUM_IDLE_PROCESSES", "1"))

# Latency-tuned defaults (override in .env if needed)
LK_STT_MODEL = os.getenv("LK_STT_MODEL", "deepgram/flux-general")
LK_LLM_MODEL = os.getenv("LK_LLM_MODEL", "google/gemma-4-31b-it")
LK_TTS_MODEL = os.getenv("LK_TTS_MODEL", "cartesia/sonic-turbo")
LK_TTS_VOICE = os.getenv(
    "LK_TTS_VOICE",
    "638efaaa-4d0c-442e-b701-3fae16aad012",  # Cartesia Sameer (en-IN, male)
)
# Natural pace — "fast" made speech sound choppy/broken.
LK_TTS_SPEED = os.getenv("LK_TTS_SPEED", "normal")

COMPANY_NAME = os.getenv("COMPANY_NAME", "our company")
COMPANY_PITCH = os.getenv(
    "COMPANY_PITCH",
    "We help businesses grow with practical tools and support.",
)
SIP_OUTBOUND_TRUNK_ID = os.getenv("SIP_OUTBOUND_TRUNK_ID", "").strip()
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "").strip()

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")

# Fixed greeting — spoken with TTS only (no LLM) so pickup feels instant.
OPENING_LINE = (
    f"Hi, this is a quick call from {COMPANY_NAME}. "
    "I tried you by email — do you have a moment?"
)

AGENT_INSTRUCTIONS = f"""You are a friendly outbound call agent for {COMPANY_NAME}.

This is a live phone call. Speak like a real person on the phone.

Pace & style (critical):
- Reply in ONE short sentence whenever possible (max two).
- Never pad with filler ("um", "sure", "absolutely", "great question").
- Sound warm and natural, not scripted.
- Never mention that you are an AI, tools, or databases.

Goals:
1. Learn if they have a company / what they do.
2. Light pitch: {COMPANY_PITCH}
3. If interested, ask how best to follow up.

If they're not interested or ask to stop, apologize briefly, thank them, and call end_call.
If you hit voicemail, call end_call immediately — no long message.
"""


def _parse_dial_info(raw: str | None) -> dict[str, Any]:
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"phone_number": raw.strip()}
    if isinstance(data, str):
        return {"phone_number": data}
    if isinstance(data, dict):
        return data
    return {}


class SalesOutreachAgent(Agent):
    def __init__(self, *, greet_on_enter: bool = False) -> None:
        self._greet_on_enter = greet_on_enter
        super().__init__(instructions=AGENT_INSTRUCTIONS)

    @function_tool
    async def end_call(self, context: RunContext) -> str:
        """End the phone call politely when the conversation is finished."""
        await context.wait_for_playout()
        job_ctx = get_job_context()
        if job_ctx is not None:
            await job_ctx.api.room.delete_room(
                api.DeleteRoomRequest(room=job_ctx.room.name)
            )
        return "Call ended."

    async def on_enter(self) -> None:
        if not self._greet_on_enter:
            return
        # TTS-only greeting — skip LLM round-trip on session start.
        self.session.say(OPENING_LINE)


server = AgentServer(
    initialize_process_timeout=60.0,
    num_idle_processes=NUM_IDLE_PROCESSES,
)


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}

    dial_info = _parse_dial_info(ctx.job.metadata)
    phone_number = (dial_info.get("phone_number") or "").strip()
    contact_name = (dial_info.get("contact_name") or "").strip()

    agent = SalesOutreachAgent(greet_on_enter=not bool(phone_number))

    # Parse TTS speed: "fast"/"normal"/"slow" or a float like 1.1
    try:
        tts_speed: str | float = float(LK_TTS_SPEED)
    except ValueError:
        tts_speed = LK_TTS_SPEED

    session = AgentSession(
        # VAD kept for barge-in; Flux (STT) owns end-of-turn — much snappier than TurnDetector.
        vad=inference.VAD(),
        stt=inference.STT(
            model=LK_STT_MODEL,
            language="en",
            extra_kwargs={
                # Lower = start the reply sooner (Flux speculative EOT).
                "eager_eot_threshold": 0.4,
                "eot_threshold": 0.6,
                "eot_timeout_ms": 1200,
            },
        ),
        llm=inference.LLM(model=LK_LLM_MODEL),
        tts=inference.TTS(
            model=LK_TTS_MODEL,
            voice=LK_TTS_VOICE,
            extra_kwargs={"speed": tts_speed},
        ),
        turn_handling={
            "turn_detection": "stt",
            "endpointing": {"min_delay": 0.0, "max_delay": 0.4},
            "preemptive_generation": {"enabled": True, "preemptive_tts": True},
            "interruption": {"enabled": True, "min_duration": 0.25},
        },
    )

    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(),
    )
    await ctx.connect()

    if not phone_number:
        logger.info("No phone_number in job metadata — console / non-dial session")
        return

    if not SIP_OUTBOUND_TRUNK_ID:
        raise RuntimeError("SIP_OUTBOUND_TRUNK_ID is missing in .env")

    sip_identity = phone_number
    logger.info(
        "Dialing %s via trunk %s from %s (llm=%s)",
        phone_number,
        SIP_OUTBOUND_TRUNK_ID,
        TWILIO_FROM_NUMBER or "(trunk default)",
        LK_LLM_MODEL,
    )

    try:
        req = api.CreateSIPParticipantRequest(
            room_name=ctx.room.name,
            sip_trunk_id=SIP_OUTBOUND_TRUNK_ID,
            sip_call_to=phone_number,
            participant_identity=sip_identity,
            wait_until_answered=True,
            play_dialtone=True,
        )
        if TWILIO_FROM_NUMBER:
            req.sip_number = TWILIO_FROM_NUMBER
        await ctx.api.sip.create_sip_participant(req)
        participant = await ctx.wait_for_participant(identity=sip_identity)
        logger.info("Callee joined: %s", participant.identity)
    except Exception as e:
        logger.exception("Outbound dial failed: %s", e)
        ctx.shutdown()
        return

    # Instant opening: TTS only (no LLM round-trip after pickup).
    opening = OPENING_LINE
    if contact_name:
        opening = (
            f"Hi {contact_name}, this is a quick call from {COMPANY_NAME}. "
            "I tried you by email — do you have a moment?"
        )
    session.say(opening)


async def dispatch_call(phone_number: str, contact_name: str = "") -> None:
    """Create a room and dispatch this agent to dial phone_number."""
    missing = [
        k
        for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
        if not os.getenv(k)
    ]
    if missing:
        print(f"Missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    if not SIP_OUTBOUND_TRUNK_ID:
        print("SIP_OUTBOUND_TRUNK_ID is not set in .env", file=sys.stderr)
        sys.exit(1)

    if not E164_RE.match(phone_number):
        print(
            f"Phone number must be E.164 (e.g. +15551234567), got: {phone_number}",
            file=sys.stderr,
        )
        sys.exit(1)

    room_name = f"outbound-{uuid.uuid4().hex[:10]}"
    metadata: dict[str, str] = {"phone_number": phone_number}
    if contact_name:
        metadata["contact_name"] = contact_name

    async with api.LiveKitAPI() as lkapi:
        dispatch = await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=AGENT_NAME,
                room=room_name,
                metadata=json.dumps(metadata),
            )
        )

    print(f"Dispatched agent '{AGENT_NAME}'")
    print(f"  room:     {room_name}")
    print(f"  calling:  {phone_number}")
    print(f"  from:     {TWILIO_FROM_NUMBER or '(trunk default)'}")
    print(f"  trunk:    {SIP_OUTBOUND_TRUNK_ID}")
    print(f"  dispatch: {dispatch.id}")
    print("Keep the worker running: uv run python voice_agent.py start")


def main() -> None:
    # Custom dialer subcommand; everything else is handed to LiveKit's CLI.
    if len(sys.argv) > 1 and sys.argv[1] == "call":
        parser = argparse.ArgumentParser(
            prog="voice_agent.py call",
            description="Place an outbound sales call",
        )
        parser.add_argument("phone_number", help="E.164 number, e.g. +15551234567")
        parser.add_argument("--name", default="", help="Optional contact name hint")
        args = parser.parse_args(sys.argv[2:])
        asyncio.run(dispatch_call(args.phone_number.strip(), args.name.strip()))
        return

    cli.run_app(server)


if __name__ == "__main__":
    main()
