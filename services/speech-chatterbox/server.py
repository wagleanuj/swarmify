"""Chatterbox Turbo speech and faster-whisper word times over HTTP, for Swarmify's speech connector.

POST /v1/audio/speech  OpenAI-style JSON {"input", "voice", "response_format": "wav", "seed"}: a WAV
                       (16-bit). The same fields as a multipart form with a `reference` file (5
                       to 10 s of speech) clone that voice; its conditioning is cached per clip
POST /v1/align         form fields `file` (audio) and `text`: {"timestamps": [{"word", "start_time",
                       "end_time"}]}, the shape Kokoro-FastAPI's captioned speech returns
GET  /health           ok once both models are loaded, with the delivery `tags` Turbo understands
                       written in brackets in the input, as "[whispering] Who sleeps?", and
                       `clips: true`, since it takes a reference clip

Both models load once at start. Chatterbox runs on the Apple GPU (MPS) when there is one, else on
the CPU; faster-whisper runs on the CPU.
"""

import hashlib
import io
import tempfile
import threading
import wave
from contextlib import asynccontextmanager
from typing import Any

import numpy as np
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError

from wordmatch import match_words

models: dict[str, Any] = {}
"""`tts` and `asr` once loaded, and `device`; a test puts fakes here and skips the lifespan."""
# ponytail: one request per model at a time; Chatterbox keeps its voice on the model object.
tts_lock, asr_lock = threading.Lock(), threading.Lock()
voices: dict[str, Any] = {}
"""Turbo's conditioning by the clip's sha256; "" is its built-in voice."""
# ponytail: kept for the process's life; a catalog holds a handful of clips.
# Turbo's added tokens (its added_tokens.json), each spoken as `[tag]`.
TAGS = [
    "whispering", "happy", "angry", "sarcastic", "surprised", "fear", "crying", "dramatic",
    "narration", "advertisement", "laugh", "chuckle", "sigh", "gasp", "groan", "cough", "sniff",
    "shush", "clear throat",
]  # fmt: skip


@asynccontextmanager
async def lifespan(_app: FastAPI):
    import torch
    from chatterbox.tts_turbo import ChatterboxTurboTTS
    from faster_whisper import WhisperModel

    models["device"] = "mps" if torch.backends.mps.is_available() else "cpu"
    models["tts"] = ChatterboxTurboTTS.from_pretrained(device=models["device"])
    models["asr"] = WhisperModel("small.en", device="cpu", compute_type="int8")
    yield


app = FastAPI(title="speech-chatterbox", lifespan=lifespan)


class Speech(BaseModel):
    input: str
    model: str = "chatterbox-turbo"
    voice: str = "default"  # accepted for OpenAI compatibility; a `reference` clip sets the voice
    response_format: str = "wav"
    seed: int | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "engine": "chatterbox-turbo",
        "device": models.get("device"),
        "tags": TAGS,
        "clips": True,
    }


def conditioned(tts: Any, clip: bytes | None) -> None:
    """Point Turbo at the clip's voice, conditioning on it the first time; None is its own."""
    key = hashlib.sha256(clip).hexdigest() if clip else ""
    voices.setdefault("", tts.conds)
    if key not in voices:
        assert clip is not None
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            f.write(clip)
            f.flush()
            try:
                tts.prepare_conditionals(f.name)
            except (AssertionError, RuntimeError, ValueError) as exc:
                raise HTTPException(400, f"the reference clip cannot be used: {exc}") from exc
        voices[key] = tts.conds
    tts.conds = voices[key]


@app.post("/v1/audio/speech")
async def speech(request: Request) -> Response:
    clip = None
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        reference = form.get("reference")
        if reference is not None and not isinstance(reference, str):
            clip = await reference.read() or None
        fields: dict[str, Any] = {k: v for k, v in form.items() if isinstance(v, str)}
    else:
        try:
            fields = await request.json()
        except ValueError as exc:
            raise HTTPException(400, "the body is neither JSON nor a multipart form") from exc
    try:
        req = Speech.model_validate(fields)
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    if req.response_format != "wav":
        raise HTTPException(400, "only response_format wav is supported")
    if not req.input.strip():
        raise HTTPException(400, "input is empty")
    return await run_in_threadpool(speak, req, clip)


def speak(req: Speech, clip: bytes | None) -> Response:
    tts = models["tts"]
    with tts_lock:
        conditioned(tts, clip)
        if req.seed is not None:
            import torch

            torch.manual_seed(req.seed)
        samples = np.asarray(tts.generate(req.input)).reshape(-1)
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(tts.sr)
        w.writeframes(pcm.tobytes())
    return Response(buf.getvalue(), media_type="audio/wav")


@app.post("/v1/align")
def align(file: UploadFile, text: str = Form()) -> dict[str, list[dict[str, Any]]]:
    with asr_lock:
        segments, info = models["asr"].transcribe(file.file, word_timestamps=True, language="en")
        heard = [(w.word.strip(), w.start, w.end) for s in segments for w in s.words or []]
    return {
        "timestamps": [
            {"word": w, "start_time": start, "end_time": end}
            for w, start, end in match_words(text, heard, info.duration)
        ]
    }
