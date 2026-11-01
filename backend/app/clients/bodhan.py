"""Bodhan AI (IIT Madras incubated, built with AI4Bharat): Indic-Translate and Indic-Speak."""

import hashlib
import io
import json
import re
import wave
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..config import settings
from ..http import fetch_bytes, fetch_json

CHAT_URL = "https://api.bodhan.ai/v1/chat/completions"
SPEECH_URL = "https://api.bodhan.ai/v1/audio/speech"
SPEECH_CHUNK_CHARS = 250  # the model card recommends short inputs for speech
SPEECH_WORKERS = 6  # Bodhan allows 10 parallel requests per key


def translation_configured() -> bool:
    return bool(settings.bodhan_translate_api_key)


def speech_configured() -> bool:
    return bool(settings.bodhan_tts_api_key)


def translate(text: str, language: str = "Tamil") -> str:
    """English -> `language`. Results are cached, so re-running a scenario costs nothing."""
    digest = hashlib.sha1(f"{language}:{text}".encode()).hexdigest()[:20]
    fetched = fetch_json(
        "Bodhan Indic-Translate", [CHAT_URL], method="POST", retries=3, timeout=60,
        headers={"Authorization": f"Bearer {settings.bodhan_translate_api_key}"},
        json_body={
            "model": "indic-translate",
            "temperature": 0,
            # Prompt format from the model card.
            "messages": [{"role": "user", "content": f"Translate the following text into {language}:\n{text}"}],
        },
        cache_key=f"bodhan-translate:{digest}",
    )
    choice = fetched.data["choices"][0]
    if choice.get("finish_reason") == "length":
        raise ValueError("translation was cut off")
    return choice["message"]["content"].strip()


def _chunks(segments: list[str]) -> list[str]:
    """Split on sentence ends so each speech request stays short. Segments never share a chunk, so
    a short first segment (the headline) comes back quickly and playback can start early."""
    chunks = []
    for segment in segments:
        sentences = [x.strip() for x in re.split(r"(?<=[.!?।])\s+", segment) if x.strip()]
        current = ""
        for sentence in sentences:
            if current and len(current) + len(sentence) + 1 > SPEECH_CHUNK_CHARS:
                chunks.append(current)
                current = sentence
            else:
                current = f"{current} {sentence}".strip()
        if current:
            chunks.append(current)
    return chunks


def speak(
    segments: list[str], lang: str = "ta", on_chunk: Callable[[int, int, str], None] | None = None,
) -> Path:
    """Tamil speech as one WAV file; returns its path. Identical text reuses the saved files.

    Chunks are synthesized in parallel and each is saved as `<digest>-<i>.wav` the moment it
    arrives; `on_chunk(index, total, file_name)` lets a caller start playing before all are done.
    """
    chunks = _chunks(segments)
    if not chunks:
        raise ValueError("no text to speak")
    digest = hashlib.sha1(f"{settings.bodhan_voice}:{lang}:{chr(10).join(chunks)}".encode()).hexdigest()[:20]
    folder = Path(settings.audio_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}.wav"

    def synthesize(index: int) -> tuple[int, bytes]:
        part = folder / f"{digest}-{index}.wav"
        if part.exists():
            return index, part.read_bytes()
        audio = fetch_bytes(
            "Bodhan Indic-Speak", SPEECH_URL,
            headers={"Authorization": f"Bearer {settings.bodhan_tts_api_key}"},
            json_body={
                "model": "indic-speak", "input": chunks[index], "voice": settings.bodhan_voice,
                "instructions": json.dumps({"lang": lang}),
            },
        )
        part.write_bytes(audio)
        return index, audio

    clips: list[bytes | None] = [None] * len(chunks)
    with ThreadPoolExecutor(max_workers=SPEECH_WORKERS) as pool:
        for future in as_completed([pool.submit(synthesize, i) for i in range(len(chunks))]):
            index, audio = future.result()
            clips[index] = audio
            if on_chunk:
                on_chunk(index, len(chunks), f"{digest}-{index}.wav")

    if not path.exists():
        params, frames = None, []
        for audio in clips:
            with wave.open(io.BytesIO(audio)) as part:
                params = params or part.getparams()
                frames.append(part.readframes(part.getnframes()))
        with wave.open(str(path), "wb") as out:
            out.setparams(params)
            for f in frames:
                out.writeframes(f)
    return path
