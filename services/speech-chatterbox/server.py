"""Chatterbox Turbo speech and faster-whisper word times over HTTP, for Swarmify's speech connector.

POST /v1/audio/speech  OpenAI-style JSON {"input", "voice", "response_format": "wav", "seed"}: a WAV
POST /v1/align         form fields `file` (audio) and `text`: {"timestamps": [{"word", "start_time",
                       "end_time"}]}, the shape Kokoro-FastAPI's captioned speech returns
GET  /health           ok once both models are loaded

Both models load once at start. Chatterbox runs on the Apple GPU (MPS) when there is one, else on
the CPU; faster-whisper runs on the CPU.
"""

import io
import threading
from contextlib import asynccontextmanager
from typing import Any

import soundfile as sf
import torch
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from wordmatch import match_words

DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"
models: dict[str, Any] = {}
# ponytail: one request per model at a time; Chatterbox keeps its voice on the model object.
tts_lock, asr_lock = threading.Lock(), threading.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from chatterbox.tts_turbo import ChatterboxTurboTTS
    from faster_whisper import WhisperModel

    models["tts"] = ChatterboxTurboTTS.from_pretrained(device=DEVICE)
    models["asr"] = WhisperModel("small.en", device="cpu", compute_type="int8")
    yield


app = FastAPI(title="speech-chatterbox", lifespan=lifespan)


class Speech(BaseModel):
    input: str
    model: str = "chatterbox-turbo"
    voice: str = "default"  # accepted for OpenAI compatibility; Turbo speaks its built-in voice
    response_format: str = "wav"
    seed: int | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "engine": "chatterbox-turbo", "device": DEVICE}


@app.post("/v1/audio/speech")
def speech(req: Speech) -> Response:
    if req.response_format != "wav":
        raise HTTPException(400, "only response_format wav is supported")
    if not req.input.strip():
        raise HTTPException(400, "input is empty")
    tts = models["tts"]
    with tts_lock:
        if req.seed is not None:
            torch.manual_seed(req.seed)
        wav = tts.generate(req.input).squeeze(0).numpy()
    buf = io.BytesIO()
    sf.write(buf, wav, tts.sr, format="WAV")
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
