"""Runs the graph for one request and turns the final state into the API's JSON shape."""

import html
import time
from dataclasses import asdict
from typing import Any

from shapely.geometry import mapping

from ..clients import bodhan, telegram
from ..events import Run, bind, emit
from ..http import ToolError
from . import safe_route as safe_route_module
from .graph import graph
from .state import FloodState


def execute(run: Run) -> None:
    with bind(run):
        emit("run_start", "Agent started")
        try:
            final = graph.invoke({"request": run.request, "sources": []}, config={"configurable": {"run": run}})
            run.state = final
            run.result = serialize(final)
        except ToolError as exc:
            run.result = {"error": str(exc)}
            run.status = "failed"
            emit("failed", f"A required data source did not respond: {exc}. Try again in a minute.")
            run.finished = True
            return
        except Exception as exc:  # surfaced to the UI instead of leaving the run hanging
            run.result = {"error": repr(exc)}
            run.status = "failed"
            emit("failed", f"Unexpected error: {exc!r}")
            run.finished = True
            return
        outcome = final.get("outcome")
        run.status = "awaiting_approval" if outcome == "alert" else "all_clear"
        emit("done", "Alert drafted, waiting for officer approval" if outcome == "alert" else "All clear",
             status=run.status)
        try:
            alert = run.result.get("alert") or {}
            if alert.get("body_ta") and bodhan.speech_configured():
                _voice(run)
        finally:
            run.finished = True


def _voice(run: Run) -> str:
    """Tamil voice alert, streamed: every chunk is announced as soon as Bodhan returns it."""
    alert = run.result["alert"]
    segments = [alert["headline_ta"], alert["body_ta"], *alert.get("actions_ta", [])]
    audio = run.result["audio"] = {"status": "generating", "chunks": [], "url": None}
    emit("voice_start", "Generating the Tamil voice alert with Bodhan Indic-Speak")

    def on_chunk(index: int, total: int, name: str) -> None:
        if not audio["chunks"]:
            audio["chunks"] = [None] * total
        audio["chunks"][index] = f"/audio/{name}"
        emit("voice_chunk", f"Voice part {index + 1} of {total} ready", index=index, total=total,
             url=f"/audio/{name}")

    try:
        path = bodhan.speak(segments, on_chunk=on_chunk)
    except (ToolError, ValueError) as exc:
        audio["status"] = "failed"
        emit("warning", f"Tamil voice alert failed ({exc}); the text alert is unaffected")
        raise
    audio.update(status="ready", url=f"/audio/{path.name}")
    emit("voice_ready", f"Tamil voice alert ready ({path.stat().st_size // 1024} KB)", url=audio["url"])
    return audio["url"]


def approve(run: Run) -> dict[str, Any]:
    """Record the officer's approval, then post the alert to the Telegram channel if configured.

    Approval always stands; a Telegram failure is reported alongside it, not instead of it.
    """
    alert = run.result["alert"]
    approved_at = time.time()
    run.result["approved_at"] = approved_at
    run.status = "approved"
    delivery: dict[str, Any] = {"configured": telegram.configured(), "posts": [], "error": None}
    with bind(run):
        emit("approved", "Officer approved the alert for release")
        if telegram.configured():
            langs = ["en"] + (["ta"] if alert.get("body_ta") else [])
            channel = telegram.channel_url()
            try:
                for lang in langs:
                    message_id = telegram.send(_format(run, lang, approved_at))
                    delivery["posts"].append(f"{channel}/{message_id}" if channel else str(message_id))
                emit("sent", f"Posted to the Telegram channel ({' and '.join(l.upper() for l in langs)})",
                     posts=delivery["posts"])
            except ToolError as exc:
                delivery["error"] = str(exc)
                emit("warning", f"Approved, but Telegram did not accept the post: {exc}")
    run.result["telegram"] = delivery
    return {"approved_at": approved_at, "telegram": delivery}


def speech(run: Run) -> str:
    """Generate (or re-try) the Tamil voice alert on request; returns its URL."""
    with bind(run):
        return _voice(run)


def safe_route(run: Run, lat: float, lon: float, label: str) -> dict[str, Any]:
    return safe_route_module.plan(run.state, lat, lon, label)


BUILDING_COLUMNS = (
    "name", "kind", "level", "score", "locality", "address", "phone", "rain_24h_max_mm", "rain_total_mm",
    "imd_category", "elevation_m", "lat", "lon", "osm_url",
)


def buildings(run: Run) -> list[dict[str, Any]]:
    """At-risk buildings as flat rows for the CSV / GeoJSON downloads."""
    return [
        {**{k: f.get(k) for k in BUILDING_COLUMNS}, "osm_url": f"https://www.openstreetmap.org/{f['id']}"}
        for f in (run.result or {}).get("facilities_at_risk", [])
    ]


LEVEL_BADGE = {"severe": ("🔴", "SEVERE"), "high": ("🟠", "HIGH"), "moderate": ("🟡", "MODERATE")}


def _format(run: Run, lang: str, approved_at: float) -> str:
    """Telegram post (HTML): severity line, headline, body, actions, and a clear label for replays."""
    result, alert = run.result, run.result["alert"]
    levels = [f["properties"]["level"] for f in result["cells"]["features"] if f["properties"]["in_focus"]]
    worst = max(levels, key=["low", "moderate", "high", "severe"].index, default="high")
    icon, word = LEVEL_BADGE.get(worst, ("⚠️", "HIGH"))
    when = time.strftime("%d %b %Y, %H:%M", time.gmtime(approved_at + 5.5 * 3600))  # IST
    parts = []
    if result.get("replay_date"):
        # The channel is public: a replayed past event must never read as a current warning.
        parts.append(f"🧪 <b>DEMO</b> · replay of archived forecasts from {result['replay_date']}, "
                     "not a current warning")
    parts.append(f"{icon} <b>{word} FLOOD RISK</b> · Chennai" if lang == "en"
                 else f"{icon} <b>வெள்ள எச்சரிக்கை</b> · சென்னை")
    parts.append(f"<b>{html.escape(alert[f'headline_{lang}'])}</b>")
    parts.append(html.escape(alert[f"body_{lang}"]))
    actions = alert.get(f"actions_{lang}", [])
    if actions:
        parts.append("\n".join(f"• {html.escape(a)}" for a in actions))
    parts.append(f"<i>Approved {when} IST · Chennai Flood Agent</i>")
    return "\n\n".join(parts)


def _latest_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for s in sources:
        if s["source"] not in latest or s["fetched_at"] > latest[s["source"]]["fetched_at"]:
            latest[s["source"]] = s
    return sorted(latest.values(), key=lambda s: s["source"])


def serialize(state: FloodState) -> dict[str, Any]:
    cells = {c.id: c for c in state["cells"]}
    localities = state.get("localities", {})
    rain = state["rain"]
    features = [
        {
            "type": "Feature",
            "geometry": mapping(cells[r.cell_id].polygon),
            "properties": {**asdict(r), "locality": localities.get(r.cell_id)},
        }
        for r in state["risks"]
    ]
    by_cell = dict(zip((c.id for c in state["cells"]), rain.per_point))
    return {
        "outcome": state.get("outcome"),
        "rain_hourly": {
            "times": rain.times,
            "by_cell": {cid: [round(v, 1) for v in series] for cid, series in by_cell.items()},
        },
        "window": {"start": rain.times[0], "end": rain.times[-1], "hours": state["horizon_hours"]},
        "replay_date": state.get("replay_date"),
        "focus": state.get("focus"),
        "boundary": mapping(state["boundary"]),
        "cells": {"type": "FeatureCollection", "features": features},
        "facilities_at_risk": state.get("facilities_at_risk", []),
        "routes": state.get("routes", []),
        "alert": state.get("alert"),
        "facts": state.get("facts"),
        "sources": _latest_sources(state.get("sources", [])),
    }
