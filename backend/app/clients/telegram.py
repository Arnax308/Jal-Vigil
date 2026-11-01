"""Telegram Bot API delivery for approved alerts, posted to a public channel."""

from ..config import settings
from ..http import fetch_json

SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


def configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def channel_url() -> str | None:
    """Public link people can join: TELEGRAM_CHANNEL_URL, or derived from an @handle chat id."""
    if settings.telegram_channel_url:
        return settings.telegram_channel_url.rstrip("/")
    if settings.telegram_chat_id.startswith("@"):
        return f"https://t.me/{settings.telegram_chat_id[1:]}"
    return None


def send(html: str) -> int:
    """Send one HTML-formatted message to the configured chat; returns Telegram's message id."""
    fetched = fetch_json(
        "Telegram Bot API", [SEND_URL.format(token=settings.telegram_bot_token)], method="POST",
        json_body={
            "chat_id": settings.telegram_chat_id, "text": html, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        retries=1,
    )
    return fetched.data["result"]["message_id"]
