"""Fact checks on generated text.

- check_alert: every number the LLM prints in the English alert (rainfall, counts, distances,
  helplines, dates) must come from the facts it was given.
- check_translation: the Tamil version must be in Tamil script and carry exactly the same numbers
  as the English it was translated from, so a dropped or altered helpline is caught.
"""

import re
from typing import Any

ALERT_FIELDS = ("headline_en", "body_en", "actions_en")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")  # \d also matches Tamil digits (௦-௯)
_TAMIL = re.compile(r"[஀-௿]")


def _norm(token: str) -> str:
    token = token.replace(",", "")
    try:
        value = float(token)
    except ValueError:
        return token
    return f"{value:.1f}".rstrip("0").rstrip(".")


def numbers_in(text: str) -> set[str]:
    return {_norm(t) for t in _NUMBER.findall(text)}


def allowed_numbers(facts: Any) -> set[str]:
    allowed: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            allowed.update({_norm(str(round(value))), _norm(f"{value:.1f}")})
        elif isinstance(value, str):
            allowed.update(numbers_in(value))
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(k)
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)

    walk(facts)
    return allowed


def _text(value: Any) -> str:
    return " ".join(value) if isinstance(value, list) else str(value or "")


def check_alert(alert: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    """Return a list of problems with the English alert; empty means it passed."""
    problems = []
    for field in ALERT_FIELDS:
        if not _text(alert.get(field)).strip():
            problems.append(f"missing field {field}")
    if "actions_en" in alert and not isinstance(alert["actions_en"], list):
        problems.append("actions_en must be a list of strings")

    found = set().union(*(numbers_in(_text(alert.get(f))) for f in ALERT_FIELDS))
    unsupported = sorted(found - allowed_numbers(facts))
    if unsupported:
        problems.append(f"numbers not present in the facts: {', '.join(unsupported)}")
    return problems


def check_translation(english: str, tamil: str) -> list[str]:
    """Return problems with one translated field; empty means it passed."""
    problems = []
    if not _TAMIL.search(tamil):
        problems.append("output is not in Tamil script")
    missing = sorted(numbers_in(english) - numbers_in(tamil))
    extra = sorted(numbers_in(tamil) - numbers_in(english))
    if missing:
        problems.append(f"numbers lost in translation: {', '.join(missing)}")
    if extra:
        problems.append(f"numbers added in translation: {', '.join(extra)}")
    return problems
