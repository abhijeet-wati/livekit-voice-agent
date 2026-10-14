import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import AsyncIterable, Literal, Optional

from dotenv import load_dotenv

load_dotenv()  # before importing our modules, so they see .env values

from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    AgentStateChangedEvent,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    ModelSettings,
    RunContext,
    function_tool,
    inference,
    llm,
    metrics,
    room_io,
    stt,
    tts,
)
from livekit.plugins import noise_cancellation, sarvam, silero

from prompts import GREETING, SINTEX_SYSTEM_PROMPT
from tools import SintexAPI, describe_claim, normalize_invoice, normalize_phone

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# Language handling
# ---------------------------------------------------------------------------

Language = Literal["english", "hinglish", "gujlish", "tanglish"]
HandoffReason = Literal[
    "claim_not_found", "account_not_found", "caller_requested",
    "caller_frustrated", "repeated_misunderstanding", "out_of_scope",
]

# The LLM writes Hindi in Devanagari, Gujarati in Gujarati script and Tamil in
# Tamil script (prompt Section 4.4). We read the script of each reply and point
# the TTS voice at the matching language before it speaks.
_SCRIPT_TO_TTS_LANG = (
    ("hi-IN", 0x0900, 0x097F),  # Devanagari
    ("gu-IN", 0x0A80, 0x0AFF),  # Gujarati
    ("ta-IN", 0x0B80, 0x0BFF),  # Tamil
)
# How much of a reply to look at before deciding it's plain English.
_SCRIPT_PEEK_CHARS = 80


def detect_tts_language(text: str) -> Optional[str]:
    for ch in text:
        cp = ord(ch)
        for code, lo, hi in _SCRIPT_TO_TTS_LANG:
            if lo <= cp <= hi:
                return code
    return None


# ---------------------------------------------------------------------------
# Speech models
# ---------------------------------------------------------------------------

STT_KEYTERMS = "Sintex rewards. Terms: Sintex, invoice ID, claim status, Bronze, Silver, points, WhatsApp, QR code."


def build_sarvam_stt() -> stt.STT:
    # Auto-detects English / Hindi / Gujarati / Tamil on every turn and keeps
    # Hindi-English code-mixing intact. Sarvam's own server-side VAD ends turns.
    return sarvam.STTStreaming(
        language="auto",
        # "verbatim" keeps words as spoken. "codemix" normalises them, which
        # turned a spoken "No" into "9" (Hindi "nau"). Our tools convert digit
        # words to numbers themselves, so no normalisation is needed.
        mode=os.getenv("STT_MODE", "verbatim"),
        stream_type="fast",
        prompt=STT_KEYTERMS,
        # Callers pause while reading out digits; 600 ms avoids cutting them off.
        vad_min_silence_ms=int(os.getenv("STT_MIN_SILENCE_MS", "600")),
        # Ignore background noise, coughs and speaker echo.
        vad_sot_threshold=float(os.getenv("STT_VAD_THRESHOLD", "0.6")),
        vad_min_speech_ms=int(os.getenv("STT_MIN_SPEECH_MS", "150")),
    )


def build_sarvam_tts() -> sarvam.TTS:
    return sarvam.TTS(
        model=os.getenv("SARVAM_TTS_MODEL", "bulbul:v3"),
        # Female voice; the prompt uses feminine verb forms to match. If you
        # switch to a male speaker (e.g. "shubh"), update prompt Section 4.2.
        speaker=os.getenv("SARVAM_TTS_SPEAKER", "simran"),
        target_language_code="en-IN",  # switched per reply in tts_node
        pace=0.95,
        min_buffer_size=30,
    )


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------


def build_instructions(caller_id_known: bool) -> str:
    today = datetime.now(IST)
    lines = [
        "",
        "## 12. LIVE CALL CONTEXT",
        "",
        f"- Today's date is {today:%A, %d %B %Y} (India time). Use it to work out statement date ranges.",
    ]
    if caller_id_known:
        lines.append(
            "- The caller's mobile number is known from caller ID. The system fills it in for every "
            "action. Leave phone_number empty, never ask for the number and never read it back, "
            "unless an action returns account_found false (Section 6.1A)."
        )
    else:
        lines.append(
            "- The caller's mobile number is not known. Ask for it only when an action that needs "
            "it is required (Section 6.1A). Never ask for it to check a claim."
        )
    return SINTEX_SYSTEM_PROMPT + "\n" + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class Assistant(Agent):
    def __init__(self, api: SintexAPI, sarvam_tts: Optional[sarvam.TTS], caller_phone: Optional[str]) -> None:
        super().__init__(instructions=build_instructions(caller_phone is not None))
        self._api = api
        self._sarvam_tts = sarvam_tts
        self._tts_lang = "en-IN"
        self._caller_id_phone = caller_phone   # from SIP, may not be the registered number
        self._phone: Optional[str] = caller_phone  # number to use for lookups
        self._phone_verified = False           # True once an account lookup succeeded

    async def on_enter(self) -> None:
        self.session.say(GREETING, allow_interruptions=True)

    # --- Pick the TTS language from the script of each reply -----------------

    async def tts_node(self, text: AsyncIterable[str], model_settings: ModelSettings):
        it = text.__aiter__()
        buffered: list[str] = []
        detected: Optional[str] = None
        seen = 0
        while seen < _SCRIPT_PEEK_CHARS:
            try:
                chunk = await it.__anext__()
            except StopAsyncIteration:
                break
            buffered.append(chunk)
            seen += len(chunk)
            detected = detect_tts_language(chunk)
            if detected:
                break

        lang = detected or "en-IN"
        if self._sarvam_tts is not None and lang != self._tts_lang:
            try:
                self._sarvam_tts.update_options(target_language_code=lang)
                self._tts_lang = lang
                logger.info("TTS language -> %s", lang)
            except Exception:
                logger.exception("Could not switch TTS language to %s", lang)

        async def replay():
            for c in buffered:
                yield c
            async for c in it:
                yield c

        async for frame in Agent.default.tts_node(self, replay(), model_settings):
            yield frame

    # --- Helpers --------------------------------------------------------------

    def _resolve_phone(self, phone_number: str) -> tuple[Optional[str], Optional[dict]]:
        """Returns (phone, None) or (None, response_for_llm)."""
        raw = phone_number.strip() if phone_number else ""
        if not raw:
            if self._phone:
                return self._phone, None
            return None, {
                "status": "need_phone_number",
                "message": "Ask the caller for their 10-digit registered mobile number, then call this action again.",
            }
        phone, heard = normalize_phone(raw)
        if not phone:
            return None, {"status": "invalid_phone_number", "heard_digits": heard,
                          "digit_count": len(heard), "expected_digits": 10}
        return phone, None

    def _remember(self, phone: str, result: dict) -> None:
        if result.get("account_found") is True:
            self._phone = phone
            self._phone_verified = True
        elif result.get("account_found") is False and phone == self._phone:
            # Caller-ID number isn't registered: make the LLM ask for the real one.
            self._phone = None
            self._phone_verified = False

    # --- Tools ----------------------------------------------------------------

    @function_tool()
    async def get_reward_summary(self, context: RunContext, phone_number: str = "") -> dict:
        """Get the caller's reward points balance and membership tier. Call this whenever the
        caller asks about points, balance, rewards or tier.

        Args:
            phone_number: The caller's registered mobile number as they said it. Leave empty if it is known from caller ID or was already given in this call.
        """
        phone, err = self._resolve_phone(phone_number)
        if err:
            return err
        result = await self._api.get_reward_summary(phone)
        self._remember(phone, result)
        return result

    @function_tool()
    async def get_recent_transactions(self, context: RunContext, phone_number: str = "", limit: int = 3) -> dict:
        """Get the caller's most recent reward transactions, newest first.

        Args:
            phone_number: The caller's registered mobile number as they said it. Leave empty if already known.
            limit: Maximum number of transactions to return (1 to 10).
        """
        phone, err = self._resolve_phone(phone_number)
        if err:
            return err
        result = await self._api.get_recent_transactions(phone, max(1, min(limit, 10)))
        self._remember(phone, result)
        return result

    @function_tool()
    async def get_reward_statement(
        self, context: RunContext, phone_number: str = "", start_date: str = "", end_date: str = ""
    ) -> dict:
        """Get a summary reward statement for a date range (default: last 30 days).

        Args:
            phone_number: The caller's registered mobile number as they said it. Leave empty if already known.
            start_date: Optional start date, YYYY-MM-DD.
            end_date: Optional end date, YYYY-MM-DD.
        """
        phone, err = self._resolve_phone(phone_number)
        if err:
            return err
        result = await self._api.get_reward_statement(phone, start_date or None, end_date or None)
        self._remember(phone, result)
        return result

    @function_tool()
    async def get_claim_status(self, context: RunContext, invoice_id: str) -> dict:
        """Check the status of a purchase claim. Needs ONLY the Invoice ID, never the mobile
        number. Call this as soon as the caller has said their Invoice ID. In your reply, state
        exactly what facts_for_caller says (in the caller's language) and read back
        invoice_id_spoken so the caller can confirm the right invoice was checked.

        Args:
            invoice_id: The Invoice ID exactly as the caller said it, in any language or format (for example "72810661", "seven two eight one zero double six one", "saat do aath ek").
        """
        invoice, heard = normalize_invoice(invoice_id)
        if not invoice:
            return {"status": "invalid_invoice_id", "heard_digits": heard,
                    "digit_count": len(heard), "expected_digits": 8}
        # Only scope the lookup to a phone number we know is the registered one.
        phone = self._phone if self._phone_verified else None
        return describe_claim(await self._api.get_claim_status(invoice, phone))

    @function_tool()
    async def send_claim_link(self, context: RunContext, language: Language, phone_number: str = "") -> dict:
        """Send the caller a WhatsApp link to create a new claim by scanning the invoice QR code.

        Args:
            language: The language the caller is speaking.
            phone_number: The caller's registered mobile number as they said it. Leave empty if already known.
        """
        phone, err = self._resolve_phone(phone_number)
        if err:
            return err
        return await self._api.send_claim_link(phone, language)

    @function_tool()
    async def transfer_to_human(
        self,
        context: RunContext,
        reason: HandoffReason,
        caller_language: Language,
        summary: str,
        invoice_id: str = "",
        phone_number: str = "",
    ) -> dict:
        """Hand the call to a Sintex sales representative. Tell the caller first.

        Args:
            reason: Why the call is being handed off.
            caller_language: The language the caller is speaking.
            summary: Short English summary: what the caller wanted, their language, and what was tried.
            invoice_id: Invoice ID if the call was about a claim, otherwise empty.
            phone_number: The caller's mobile number as they said it. Leave empty if already known.
        """
        phone, err = self._resolve_phone(phone_number)
        if err:
            return err
        invoice = normalize_invoice(invoice_id)[1] if invoice_id else None
        return await self._api.transfer_to_human(phone, reason, caller_language, summary, invoice or None)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

def prewarm(proc: JobProcess) -> None:
    # Local voice activity detection, loaded once per worker process. It ignores
    # speaker echo and background noise, so the agent doesn't answer itself
    # when you demo on laptop speakers.
    proc.userdata["vad"] = silero.VAD.load(min_silence_duration=0.4)


server = AgentServer(setup_fnc=prewarm)


def _caller_phone_from(participant: rtc.RemoteParticipant) -> Optional[str]:
    # SIP calls carry the caller's number. For web tests you can set a
    # "phone_number" participant attribute when you mint the token.
    raw = participant.attributes.get("sip.phoneNumber") or participant.attributes.get("phone_number")
    if not raw:
        return None
    phone, _ = normalize_phone(raw)
    return phone


@server.rtc_session()
async def entrypoint(ctx: JobContext):
    usage_collector = metrics.UsageCollector()
    last_eou_metrics: metrics.EOUMetrics | None = None

    if ctx.is_fake_job():
        # `console` mode: no real room or participant, so no caller ID.
        is_sip, caller_phone = False, None
    else:
        await ctx.connect()
        participant = await ctx.wait_for_participant()
        is_sip = participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        caller_phone = _caller_phone_from(participant)
    logger.info("Caller joined (sip=%s, caller_id_known=%s)", is_sip, caller_phone is not None)

    api = SintexAPI()
    sarvam_tts = build_sarvam_tts()

    session = AgentSession(
        llm=llm.FallbackAdapter([
            inference.LLM(model="openai/gpt-4.1-mini"),
            inference.LLM(model="google/gemini-2.5-flash"),
        ]),
        stt=stt.FallbackAdapter([
            build_sarvam_stt(),
            inference.STT.from_model_string("deepgram/nova-3:multi"),  # Hindi+English backup
        ]),
        tts=tts.FallbackAdapter([
            sarvam_tts,
            inference.TTS.from_model_string("cartesia/sonic-3:9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"),
        ]),
        # Local Silero VAD decides when someone is actually speaking, and the
        # LiveKit turn detector (supports Hindi) decides when they've finished.
        # Sarvam is used only for transcription.
        vad=ctx.proc.userdata["vad"],
        turn_handling={
            "turn_detection": inference.TurnDetector(),
            "endpointing": {"mode": "fixed", "min_delay": 0.5, "max_delay": 3.0},
            "interruption": {
                "enabled": True,
                "mode": "vad",
                "min_duration": 0.5,  # half a second of real speech to interrupt
                "min_words": 1,
                "false_interruption_timeout": 1.5,
                "resume_false_interruption": True,
            },
            "preemptive_generation": {"enabled": True},
        },
        aec_warmup_duration=None if is_sip else 3.0,
    )

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent):
        nonlocal last_eou_metrics
        if ev.metrics.type == "eou_metrics":
            last_eou_metrics = ev.metrics
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    @session.on("agent_state_changed")
    def _on_agent_state_changed(ev: AgentStateChangedEvent):
        if ev.new_state == "speaking" and last_eou_metrics:
            elapsed = time.time() - last_eou_metrics.timestamp
            logger.info(f"Time to first audio: {elapsed:.3f}s")

    @session.on("user_input_transcribed")
    def _on_transcript(ev):
        if ev.is_final:
            logger.info("USER [%s]: %s", getattr(ev, "language", "?"), ev.transcript)

    async def cleanup():
        logger.info("Usage summary: %s", usage_collector.get_summary())
        await api.aclose()

    ctx.add_shutdown_callback(cleanup)

    await session.start(
        agent=Assistant(api=api, sarvam_tts=sarvam_tts, caller_phone=caller_phone),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=noise_cancellation.BVCTelephony() if is_sip else noise_cancellation.BVC(),
            ),
        ),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agents.cli.run_app(server)