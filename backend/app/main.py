"""HTTP API and static frontend.

POST /api/runs                 start an agent run
GET  /api/runs/{id}/events     live trace (Server-Sent Events)
GET  /api/runs/{id}            final result
POST /api/runs/{id}/approve    officer approval (Telegram delivery on hold)
POST /api/runs/{id}/speech     re-try the Tamil voice alert (normally generated automatically)
POST /api/runs/{id}/route      safe route from a point or place to the nearest relief site
GET  /api/runs/{id}/buildings.csv | .geojson   at-risk buildings for download
"""

import asyncio
import csv
import io
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .agent import runner
from .clients import bodhan, llm, osm, telegram
from .clients.openmeteo import REPLAY_EARLIEST
from .config import ROOT, settings
from .events import Run, registry
from .http import ToolError

app = FastAPI(title=settings.app_name)
_background: set[asyncio.Task] = set()


class RunRequest(BaseModel):
    mode: Literal["live", "replay"] = "live"
    replay_date: date | None = None
    horizon_hours: Literal[24, 48, 72] = 72
    query: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def check_replay(self) -> "RunRequest":
        if self.mode == "live":
            self.replay_date = None
            return self
        latest = date.today() - timedelta(days=3)
        if self.replay_date is None:
            raise ValueError("replay mode needs a replay_date")
        if not REPLAY_EARLIEST <= self.replay_date <= latest:
            raise ValueError(f"replay_date must be between {REPLAY_EARLIEST} and {latest}")
        return self


def _run_or_404(run_id: str) -> Run:
    run = registry.get(run_id)
    if run is None:
        raise HTTPException(404, "Unknown run id (runs are kept in memory and reset on restart)")
    return run


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "app_name": settings.app_name,
        "llm_providers": [name for name, _ in llm.providers()],
        "routing": "Valhalla (avoids risk cells) + OSRM fallback",
        "tamil_translation": bodhan.translation_configured(),
        "tamil_speech": bodhan.speech_configured(),
        "telegram": telegram.configured(),
        "replay_range": [REPLAY_EARLIEST.isoformat(), (date.today() - timedelta(days=3)).isoformat()],
    }


@app.post("/api/runs", status_code=202)
async def start_run(body: RunRequest) -> dict:
    run = registry.create(body.model_dump(mode="json"))
    task = asyncio.create_task(asyncio.to_thread(runner.execute, run))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"run_id": run.id}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = _run_or_404(run_id)
    return {"id": run.id, "status": run.status, "request": run.request, "result": run.result}


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request) -> StreamingResponse:
    run = _run_or_404(run_id)
    last_id = request.headers.get("last-event-id")
    start = int(last_id) + 1 if last_id and last_id.isdigit() else 0

    async def events():
        seq = start
        while not await request.is_disconnected():
            for event in run.events_since(seq):
                seq = event["seq"] + 1
                yield f"id: {event['seq']}\ndata: {json.dumps(event, default=str)}\n\n"
            if run.finished and seq >= len(run.events):
                yield f"event: end\ndata: {json.dumps({'status': run.status})}\n\n"
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/runs/{run_id}/approve")
async def approve_run(run_id: str) -> dict:
    run = _run_or_404(run_id)
    if run.status != "awaiting_approval":
        raise HTTPException(409, f"Run is '{run.status}', only drafted alerts can be approved")
    try:
        outcome = await asyncio.to_thread(runner.approve, run)
    except ToolError as exc:
        raise HTTPException(502, f"Approved, but Telegram did not accept the message: {exc}") from exc
    return {"status": run.status, **outcome}


@app.post("/api/runs/{run_id}/speech")
async def speech(run_id: str) -> dict:
    run = _run_or_404(run_id)
    alert = (run.result or {}).get("alert") or {}
    if not alert.get("body_ta"):
        raise HTTPException(409, "This alert has no checked Tamil version to read aloud")
    if not bodhan.speech_configured():
        raise HTTPException(400, "Tamil speech is not configured: set BODHAN_TTS_API_KEY")
    try:
        url = await asyncio.to_thread(runner.speech, run)
    except (ToolError, ValueError) as exc:
        raise HTTPException(502, f"Bodhan Indic-Speak failed: {exc}") from exc
    return {"url": url}


class RouteRequest(BaseModel):
    lat: float | None = None
    lon: float | None = None
    place: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def check_start(self) -> "RouteRequest":
        if self.place is None and (self.lat is None or self.lon is None):
            raise ValueError("give a place name or both lat and lon")
        return self


@app.post("/api/runs/{run_id}/route")
async def safe_route(run_id: str, body: RouteRequest) -> dict:
    run = _run_or_404(run_id)
    if run.state is None or run.state.get("outcome") != "alert":
        raise HTTPException(409, "Safe routes are planned only for runs that found high risk")
    lat, lon, label = body.lat, body.lon, "Selected point"
    if body.place:
        try:
            hit, _ = await asyncio.to_thread(osm.geocode_in_city, body.place.strip())
        except ToolError as exc:
            raise HTTPException(502, f"Place lookup failed: {exc}") from exc
        if hit is None:
            raise HTTPException(404, f"Could not find '{body.place}' inside Chennai")
        lat, lon, label = hit["lat"], hit["lon"], hit["name"]
    south, west, north, east = settings.bbox
    if not (south <= lat <= north and west <= lon <= east):
        raise HTTPException(400, "That point is outside the Chennai area this agent covers")
    try:
        return await asyncio.to_thread(runner.safe_route, run, lat, lon, label)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ToolError as exc:
        raise HTTPException(502, f"Routing failed: {exc}") from exc


def _download(content: str, media_type: str, filename: str) -> Response:
    return Response(content, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/api/runs/{run_id}/buildings.csv")
def buildings_csv(run_id: str) -> Response:
    rows = runner.buildings(_run_or_404(run_id))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=runner.BUILDING_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    # BOM so Excel opens Tamil / special characters in names correctly.
    return _download("\ufeff" + out.getvalue(), "text/csv; charset=utf-8", f"chennai-at-risk-buildings-{run_id}.csv")


@app.get("/api/runs/{run_id}/buildings.geojson")
def buildings_geojson(run_id: str) -> Response:
    rows = runner.buildings(_run_or_404(run_id))
    features = [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
         "properties": {k: v for k, v in r.items() if k not in ("lat", "lon")}}
        for r in rows
    ]
    return _download(json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False),
                     "application/geo+json", f"chennai-at-risk-buildings-{run_id}.geojson")


Path(settings.audio_dir).mkdir(parents=True, exist_ok=True)
app.mount("/audio", StaticFiles(directory=settings.audio_dir), name="audio")
app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
