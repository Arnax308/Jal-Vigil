"""JSON-mode chat completions: Groq first, Gemini as fallback."""

import json
from typing import Any

from ..config import settings
from ..events import emit
from ..http import ToolError, fetch_json

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMUnavailable(Exception):
    pass


def _groq(system: str, user: str) -> str:
    fetched = fetch_json(
        "Groq", [GROQ_URL], method="POST", retries=1, timeout=45,
        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        json_body={
            "model": settings.groq_model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        },
    )
    return fetched.data["choices"][0]["message"]["content"]


def _gemini(system: str, user: str) -> str:
    fetched = fetch_json(
        "Gemini", [GEMINI_URL.format(model=settings.gemini_model)], method="POST", retries=1, timeout=45,
        headers={"x-goog-api-key": settings.gemini_api_key},
        json_body={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        },
    )
    return fetched.data["candidates"][0]["content"]["parts"][0]["text"]


def providers() -> list[tuple[str, Any]]:
    found = []
    if settings.groq_api_key:
        found.append((f"groq:{settings.groq_model}", _groq))
    if settings.gemini_api_key:
        found.append((f"gemini:{settings.gemini_model}", _gemini))
    return found


def chat_json(system: str, user: str) -> tuple[dict[str, Any], str]:
    """Return the parsed JSON reply and the name of the model that produced it."""
    available = providers()
    if not available:
        raise LLMUnavailable("no LLM API key configured (GROQ_API_KEY or GEMINI_API_KEY)")
    errors = []
    for name, call in available:
        try:
            reply = call(system, user)
            return json.loads(reply), name
        except (ToolError, KeyError, IndexError, json.JSONDecodeError) as exc:
            errors.append(f"{name}: {exc}")
            emit("retry", f"LLM {name} failed ({exc}); trying next provider")
    raise LLMUnavailable("; ".join(errors))
