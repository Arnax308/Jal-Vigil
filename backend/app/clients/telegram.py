"""Telegram Bot API delivery for approved alerts."""

from ..config import settings
from ..http import fetch_json

SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


def configured() -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def send(text: str) -> int:
    """Send one message to the configured chat; returns Telegram's message id."""
    fetched = fetch_json(
        "Telegram Bot API", [SEND_URL.format(token=settings.telegram_bot_token)], method="POST",
        json_body={"chat_id": settings.telegram_chat_id, "text": text, "disable_web_page_preview": True},
        retries=1,
    )
    return fetched.data["result"]["message_id"]
