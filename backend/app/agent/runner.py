"""Runs the graph for one request and turns the final state into the API's JSON shape."""

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
    """Record the officer's approval. Telegram delivery is on hold and runs only if configured."""
    alert = run.result["alert"]
    approved_at = time.time()
    run.result["approved_at"] = approved_at
    with bind(run):
        emit("approved", "Officer approved the alert for release")
        sent: list[int] = []
        if telegram.configured():
            messages = [_format(alert, "en")] + ([_format(alert, "ta")] if alert.get("body_ta") else [])
            sent = [telegram.send(text) for text in messages]
            emit("sent", f"{len(sent)} Telegram message(s) delivered")
    run.status = "approved"
    return {"approved_at": approved_at, "telegram_message_ids": sent}


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


def _format(alert: dict[str, Any], lang: str) -> str:
    actions = "\n".join(f"- {a}" for a in alert.get(f"actions_{lang}", []))
    return f"{alert[f'headline_{lang}']}\n\n{alert[f'body_{lang}']}\n\n{actions}".strip()


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
