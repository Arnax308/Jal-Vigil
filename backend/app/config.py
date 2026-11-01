"""Runtime settings, read once from the environment (a root-level .env is loaded if present)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_list(name: str, default: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in _env(name, default).split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    app_name: str = _env("APP_NAME", "Chennai Flood Agent")
    city_query: str = "Chennai, Tamil Nadu, India"
    timezone: str = "Asia/Kolkata"
    # south, west, north, east: the Greater Chennai bounding box as returned by Nominatim
    bbox: tuple[float, float, float, float] = (12.85, 80.13, 13.24, 80.34)
    grid_step_deg: float = float(_env("GRID_STEP_DEG", "0.025"))  # ~2.7 km cells

    user_agent: str = _env("USER_AGENT", "chennai-flood-agent/0.1 (hackathon prototype)")
    db_path: str = _env("DATABASE_PATH", str(ROOT / "data" / "cache.sqlite3"))

    groq_api_key: str = _env("GROQ_API_KEY")
    groq_model: str = _env("GROQ_MODEL", "openai/gpt-oss-120b")
    gemini_api_key: str = _env("GEMINI_API_KEY")
    gemini_model: str = _env("GEMINI_MODEL", "gemini-3.5-flash")

    # Bodhan AI (IIT Madras / AI4Bharat) hosted models; the console issues one key per model.
    bodhan_translate_api_key: str = _env("BODHAN_TRANSLATE_API_KEY")
    bodhan_tts_api_key: str = _env("BODHAN_TTS_API_KEY")
    bodhan_voice: str = _env("BODHAN_VOICE", "Anitha")  # Tamil voices: Anitha (female), Arun (male)
    audio_dir: str = _env("AUDIO_DIR", str(ROOT / "data" / "audio"))

    # Telegram delivery is on hold; approval works without it and sends only if both are set.
    telegram_bot_token: str = _env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = _env("TELEGRAM_CHAT_ID")

    overpass_urls: tuple[str, ...] = _env_list(
        "OVERPASS_URLS",
        "https://overpass-api.de/api/interpreter,"
        "https://overpass.private.coffee/api/interpreter,"
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    )

    max_routes: int = int(_env("MAX_ROUTES", "5"))
    max_named_localities: int = int(_env("MAX_NAMED_LOCALITIES", "6"))


settings = Settings()

# Public emergency numbers quoted in alerts. These are the official lines, kept here so the
# alert guardrail can treat them as known facts rather than model output.
HELPLINES = {
    "Greater Chennai Corporation helpline": "1913",
    "Tamil Nadu State Emergency Operation Centre": "1070",
    "National emergency number": "112",
}
