"""FastAPI app: JSON API for the browser page, plus the static files."""

from __future__ import annotations

import base64
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pood_thai.brain import AgentSdkBrain
from pood_thai.config import Settings
from pood_thai.conversation import ConversationManager, MessageNotFound, NoSpeech, ReviewNotFound, SessionNotFound
from pood_thai.db import Database
from pood_thai.models import (
    DEFAULT_VOICE,
    Audio,
    BrainError,
    Fact,
    Message,
    Review,
    Speaker,
    SpeechError,
    TranscriptionError,
)
from pood_thai.stt import MlxWhisperTranscriber
from pood_thai.topics import TOPICS
from pood_thai.tts import VOICES, AzureSpeaker, FallbackSpeaker, SaySpeaker, Voice

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
ALLOWED_SUFFIXES = {".webm", ".mp4", ".m4a", ".ogg", ".wav"}
# The app only serves this machine; other hosts/origins mean DNS rebinding or a cross-site request.
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


class StartRequest(BaseModel):
    topic: str
    voice: str = DEFAULT_VOICE


class SpeechRequest(BaseModel):
    voice: str = DEFAULT_VOICE


class BadRequest(Exception):
    pass


def message_json(message: Message | None) -> dict | None:
    if message is None:
        return None
    return {
        "id": message.id,
        "role": message.role,
        "lang": message.lang,
        "thai": message.thai,
        "english": message.english,
        "english_note": message.english_note,
    }


def fact_json(fact: Fact) -> dict:
    return {"id": fact.id, "fact": fact.fact}


def _topic_label(topic_id: str) -> str:
    return next((t.label for t in TOPICS if t.id == topic_id), topic_id)


def review_json(review: Review) -> dict:
    return {
        "id": review.id,
        "session_id": review.session_id,
        "topic": review.topic,
        "topic_label": _topic_label(review.topic),
        "created_at": review.created_at,
        "items": [asdict(i) for i in review.items],
        "tips": list(review.tips),
    }


def review_summary_json(review: Review) -> dict:
    return {
        "id": review.id,
        "topic": review.topic,
        "topic_label": _topic_label(review.topic),
        "created_at": review.created_at,
        "total": len(review.items),
        "fixes": sum(1 for i in review.items if not i.ok),
    }


def audio_json(audio: Audio | None) -> dict:
    if audio is None:
        return {"audio_b64": None, "audio_mime": None, "fallback_voice": False, "words": []}
    return {
        "audio_b64": base64.b64encode(audio.data).decode("ascii"),
        "audio_mime": audio.mime,
        "fallback_voice": audio.fallback_used,
        "words": [
            {"part": w.part, "start": w.start, "end": w.end, "t_start": w.t_start, "t_end": w.t_end}
            for w in audio.words
        ],
    }


def _error(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "detail": detail})


def create_app(
    manager: ConversationManager,
    db: Database,
    on_startup: Callable[[], Awaitable[None]] | None = None,
    voices: Sequence[Voice] = (),
) -> FastAPI:
    voice_ids = {v.id for v in voices}

    def check_voice(voice: str) -> str:
        # Without a catalog (no Azure key) the Mac voice ignores the choice, so anything goes.
        if voice_ids and voice not in voice_ids:
            raise BadRequest(f"Unknown voice {voice!r}")
        return voice

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if on_startup is not None:
            await on_startup()
        yield
        await manager.close_all()
        db.close()

    app = FastAPI(title="pood-thai", lifespan=lifespan)

    @app.middleware("http")
    async def _local_only(request: Request, call_next):
        host = urlsplit(f"//{request.headers.get('host', '')}").hostname
        origin = request.headers.get("origin")
        if host not in LOCAL_HOSTS or (origin is not None and urlsplit(origin).hostname not in LOCAL_HOSTS):
            return _error(403, "forbidden", "Only requests from this computer are allowed")
        return await call_next(request)

    @app.exception_handler(BadRequest)
    async def _bad_request(_: Request, exc: BadRequest):
        return _error(400, "bad_request", str(exc))

    @app.exception_handler(SessionNotFound)
    async def _not_found(_: Request, exc: SessionNotFound):
        return _error(404, "session_not_found", f"No live session {exc}")

    @app.exception_handler(MessageNotFound)
    async def _message_not_found(_: Request, exc: MessageNotFound):
        return _error(404, "message_not_found", f"No reply {exc} in this session")

    @app.exception_handler(ReviewNotFound)
    async def _review_not_found(_: Request, exc: ReviewNotFound):
        return _error(404, "review_not_found", f"No review or playable item {exc.args[0]}")

    @app.exception_handler(NoSpeech)
    async def _no_speech(_: Request, exc: NoSpeech):
        return _error(422, "no_speech", "No speech recognised")

    @app.exception_handler(BrainError)
    async def _brain(_: Request, exc: BrainError):
        logger.warning("Brain error: %s", exc)
        return _error(503, "brain_unavailable", str(exc))

    @app.exception_handler(SpeechError)
    async def _speech(_: Request, exc: SpeechError):
        logger.warning("Speech error: %s", exc)
        return _error(503, "speech_unavailable", str(exc))

    @app.exception_handler(TranscriptionError)
    async def _transcription(_: Request, exc: TranscriptionError):
        logger.warning("Transcription error: %s", exc)
        return _error(503, "transcription_failed", str(exc))

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.get("/api/topics")
    async def topics():
        return {"topics": [{"id": t.id, "label": t.label, "thai": t.thai} for t in TOPICS]}

    @app.get("/api/voices")
    async def list_voices():
        return {"voices": [{"id": v.id, "label": v.label} for v in voices], "default": DEFAULT_VOICE}

    @app.post("/api/sessions")
    async def start_session(body: StartRequest):
        voice = check_voice(body.voice)
        try:
            result = await manager.start(body.topic, voice)
        except ValueError as exc:
            raise BadRequest(str(exc)) from exc
        return {"session_id": result.session_id, "message": message_json(result.message), **audio_json(result.audio)}

    @app.post("/api/sessions/{session_id}/turns")
    async def turn(
        session_id: int,
        audio: UploadFile = File(...),
        lang: str = Form(...),
        voice: str = Form(DEFAULT_VOICE),
    ):
        if lang not in ("th", "en"):
            raise BadRequest(f"lang must be 'th' or 'en', got {lang!r}")
        check_voice(voice)
        suffix = Path(audio.filename or "").suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            suffix = ".webm"
        result = await manager.handle_audio(session_id, await audio.read(), suffix, lang, voice)
        return {
            "kind": result.kind,
            "user_message": message_json(result.user_message),
            "assistant_message": message_json(result.assistant_message),
            "replay_message_id": result.replay_message_id,
            **audio_json(result.audio),
        }

    @app.post("/api/sessions/{session_id}/messages/{message_id}/speech")
    async def speech(session_id: int, message_id: int, body: SpeechRequest):
        return audio_json(await manager.speak(session_id, message_id, check_voice(body.voice)))

    @app.post("/api/sessions/{session_id}/undo")
    async def undo(session_id: int):
        return {"removed_ids": await manager.undo(session_id)}

    @app.post("/api/sessions/{session_id}/end")
    async def end(session_id: int):
        result = await manager.end(session_id)
        return {
            "facts": [fact_json(f) for f in result.facts],
            "review": review_json(result.review) if result.review else None,
            "review_failed": result.review_failed,
        }

    @app.get("/api/reviews")
    async def list_reviews():
        return {"reviews": [review_summary_json(r) for r in db.list_reviews()]}

    @app.get("/api/reviews/{review_id}")
    async def get_review(review_id: int):
        review = db.get_review(review_id)
        if review is None:
            raise ReviewNotFound(review_id)
        return review_json(review)

    @app.post("/api/reviews/{review_id}/items/{index}/speech")
    async def review_speech(review_id: int, index: int, body: SpeechRequest):
        return audio_json(await manager.speak_review(review_id, index, check_voice(body.voice)))

    @app.get("/api/memory")
    async def memory():
        return {"facts": [fact_json(f) for f in db.list_facts()]}

    @app.delete("/api/memory/{fact_id}")
    async def delete_memory(fact_id: int):
        if not db.delete_fact(fact_id):
            return _error(404, "fact_not_found", f"No fact {fact_id}")
        return {"ok": True}

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def build_speaker(settings: Settings) -> Speaker:
    if not settings.azure_key:
        return SaySpeaker()
    return FallbackSpeaker(AzureSpeaker(settings.azure_key, settings.azure_region), SaySpeaker())


async def safe_warm_up(transcriber: MlxWhisperTranscriber) -> None:
    """Keep serving if warm-up fails; each turn then reports the transcription error to the user."""
    try:
        await transcriber.warm_up()
    except TranscriptionError:
        logger.error("Whisper warm-up failed. Is ffmpeg installed (brew install ffmpeg) and did the model download finish?")


def build_app() -> FastAPI:
    """Production wiring; used by `uvicorn --factory pood_thai.app:build_app`."""
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    transcriber = MlxWhisperTranscriber(settings.whisper_model)
    speaker = build_speaker(settings)
    brain = AgentSdkBrain(settings.claude_model, settings.data_dir.resolve())
    manager = ConversationManager(db, brain, transcriber, speaker)
    voices = list(VOICES.values()) if settings.azure_key else []
    return create_app(manager, db, on_startup=lambda: safe_warm_up(transcriber), voices=voices)
