"""Chatterbox speech and faster-whisper word times over HTTP, for Swarmify's speech connector.

POST /v1/audio/speech  OpenAI-style JSON {"input", "voice", "response_format": "wav", "seed"}: a WAV
                       (16-bit). The same fields as a multipart form with a `reference` file (5
                       to 10 s of speech) clone that voice; its conditioning is cached per model
                       and clip. `model` "chatterbox" speaks with the full model (loaded on first
                       use), which takes `exaggeration`, `cfg_weight` and `temperature`; any
                       other model is Turbo, which takes `temperature` only
POST /v1/align         form fields `file` (audio) and `text`: {"timestamps": [{"word", "start_time",
                       "end_time"}]}, the shape Kokoro-FastAPI's captioned speech returns
GET  /health           ok once both models are loaded, with the delivery `tags` Turbo understands
                       written in brackets in the input, as "[whispering] Who sleeps?", and
                       `clips: true`, since it takes a reference clip, and the `models` it serves

Turbo and faster-whisper load once at start. Chatterbox runs on an NVIDIA GPU (CUDA) or the Apple
GPU (MPS) when there is one, else on the CPU; faster-whisper runs on the CPU.
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
from pydantic import BaseModel, Field, ValidationError

from wordmatch import match_words

models: dict[str, Any] = {}
"""`tts` (Turbo) and `asr` once loaded, `full` (ChatterboxTTS) once first asked for, and `device`;
a test puts fakes here and skips the lifespan."""
# ponytail: one take at a time across both models; Chatterbox keeps its voice on the model object.
tts_lock, asr_lock = threading.Lock(), threading.Lock()
voices: dict[tuple[str, str], Any] = {}
"""Each model's conditioning by (model key, the clip's sha256); "" is the model's built-in voice."""
FULL = "chatterbox"
"""The `model` that picks the full ChatterboxTTS; anything else is Turbo."""
# ponytail: kept for the process's life; a catalog holds a handful of clips.
# Turbo's added tokens (its added_tokens.json), each spoken as `[tag]`.
TAGS = [
    "whispering", "happy", "angry", "sarcastic", "surprised", "fear", "crying", "dramatic",
    "narration", "advertisement", "laugh", "chuckle", "sigh", "gasp", "groan", "cough", "sniff",
    "shush", "clear throat",
]  # fmt: skip


def device(torch: Any) -> str:
    """Where Chatterbox runs: an NVIDIA GPU, else an Apple one, else the CPU."""
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    import torch
    from chatterbox.tts_turbo import ChatterboxTurboTTS
    from faster_whisper import WhisperModel

    models["device"] = device(torch)
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
    exaggeration: float | None = Field(None, ge=0.25, le=2.0)
    # 0 crashes chatterbox-tts 0.1.7: "Sizes of tensors must match" in t3.inference
    cfg_weight: float | None = Field(None, gt=0.0, le=1.0)
    temperature: float | None = Field(None, ge=0.05, le=2.0)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "engine": "chatterbox-turbo",
        "device": models.get("device"),
        "tags": TAGS,
        "clips": True,
        "models": ["chatterbox-turbo", FULL],
    }


def full_model() -> Any:
    """The full ChatterboxTTS, loaded the first time a request asks for it (under tts_lock)."""
    if "full" not in models:
        from chatterbox.tts import ChatterboxTTS

        models["full"] = ChatterboxTTS.from_pretrained(device=models["device"])
    return models["full"]


def conditioned(tts: Any, clip: bytes | None, model: str = "tts") -> None:
    """Point the model at the clip's voice, conditioning on it the first time; None is its own."""
    key = (model, hashlib.sha256(clip).hexdigest() if clip else "")
    voices.setdefault((model, ""), tts.conds)
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
    full = req.model == FULL
    settings = ("exaggeration", "cfg_weight", "temperature") if full else ("temperature",)
    kwargs = {k: v for k in settings if (v := getattr(req, k)) is not None}
    with tts_lock:
        tts = full_model() if full else models["tts"]
        conditioned(tts, clip, "full" if full else "tts")
        if req.seed is not None:
            import torch

            torch.manual_seed(req.seed)
        samples = np.asarray(tts.generate(req.input, **kwargs)).reshape(-1)
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
