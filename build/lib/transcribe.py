"""Voice note transcription via OpenAI Whisper.

Used by the Telegram adapter for voice notes. The caller fetches audio
bytes (e.g. via Telegram getFile) and passes them to `transcribe_audio`.
"""
from __future__ import annotations

import io
import logging
import os

import requests

log = logging.getLogger(__name__)

WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-1"
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # Whisper's documented limit


def transcribe_audio(audio_bytes: bytes, content_type: str) -> str:
    """Send audio to Whisper, return transcript text."""
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise ValueError(f"audio too large: {len(audio_bytes)} bytes")

    api_key = os.environ["OPENAI_API_KEY"]
    if "ogg" in content_type:
        ext = "ogg"
    elif "mpeg" in content_type or "mp3" in content_type:
        ext = "mp3"
    elif "wav" in content_type:
        ext = "wav"
    elif "mp4" in content_type or "m4a" in content_type:
        ext = "m4a"
    else:
        ext = "ogg"  # most messaging platforms use OGG/Opus

    files = {"file": (f"audio.{ext}", io.BytesIO(audio_bytes), content_type)}
    data = {"model": WHISPER_MODEL}
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = requests.post(WHISPER_URL, headers=headers, files=files, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json().get("text", "").strip()
