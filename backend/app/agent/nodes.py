"""Graph nodes. Each reads the state, calls tools, emits trace events and returns state updates."""

import contextvars
import json
import math
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from ..clients import bodhan, llm, openmeteo, osm, routing
from ..config import HELPLINES, settings
from ..events import bind, emit, step
from ..geo import build_grid, haversine_km
from ..guardrails import check_alert, check_translation
from ..http import ToolError
from ..risk import LEVELS, assess, max_rolling_sum
from .state import FloodState

KIND_PRIORITY = {kind: i for i, kind in enumerate(osm.FACILITY_KINDS)}
RELIEF_KINDS = ("shelter", "school", "college")
FOCUS_RADIUS_KM = 6.0
MIN_EVACUATION_KM = 1.5
ELEVATION_SAMPLES_PER_CELL = 5


def node(name: str, title: str):
    """Bind the run's event log for the node's thread and trace the node as one step.

    LangGraph passes `config` only to callables that declare it, so the wrapper keeps an explicit
    (state, config) signature instead of using functools.wraps (which would expose fn's signature).
    """
    def decorate(fn):
        def run_node(state: FloodState, config) -> dict:
            with bind(config["configurable"]["run"]), step(name, title):
                return fn(state)

        run_node.__name__ = fn.__name__
        run_node.__doc__ = fn.__doc__
        return run_node

    return decorate


# --- request -------------------------------------------------------------------------------------

INTERPRET_SYSTEM = """You turn a disaster officer's request about flooding in Chennai into parameters.
Reply with JSON: {"focus_places": [string], "horizon_hours": 24, 48, 72 or null, "reasoning": short string}.
focus_places lists each Chennai locality, street or landmark named in the request as its own item
(at most 3), otherwise [].
horizon_hours is the look-ahead the officer asked for, otherwise null. Never invent a place."""


@node("interpret_request", "Interpret request")
def interpret_request(state: FloodState) -> dict:
    req = state["request"]
    horizon = int(req.get("horizon_hours") or 72)
    replay_date = date.fromisoformat(req["replay_date"]) if req.get("replay_date") else None
    focus = None

    query = (req.get("query") or "").strip()
    if query:
        try:
            parsed, model = llm.chat_json(INTERPRET_SYSTEM, query)
            emit("reasoning", f"{model}: {parsed.get('reasoning') or 'parsed the request'}", parsed=parsed)
            if parsed.get("horizon_hours") in (24, 48, 72):
                horizon = parsed["horizon_hours"]
            places = parsed.get("focus_places") or []
            places = [p.strip() for p in places if isinstance(p, str) and p.strip()][:3]
            hits = []
            for place in places:
                hit, _ = osm.geocode_in_city(place)
                if hit:
                    hits.append(hit)
                else:
                    emit("warning", f"Could not find '{place}' inside Chennai; leaving it out")
            if hits:
                focus = _focus_around(hits)
                emit("tool_result", f"Focusing on {focus['name']} ({focus['radius_km']:.0f} km radius)", focus=focus)
            elif places:
                emit("warning", "None of the named places were found inside Chennai; analysing the whole city")
        except llm.LLMUnavailable as exc:
            emit("warning", f"Request text ignored because no LLM answered: {exc}")
        except ToolError as exc:
            emit("warning", f"Place lookup failed ({exc}); analysing the whole city")

    horizon = min(max(horizon, 24), 72)
    mode = f"replay of archived forecasts from {replay_date}" if replay_date else "live forecast"
    emit("reasoning", f"Plan: {mode}, next {horizon} h, {focus['name'] if focus else 'whole city'}")
    return {"horizon_hours": horizon, "replay_date": replay_date, "focus": focus}


def _focus_around(hits: list[dict]) -> dict:
    """One focus circle: centred between the places found, wide enough to cover each of them."""
    lat = sum(h["lat"] for h in hits) / len(hits)
    lon = sum(h["lon"] for h in hits) / len(hits)
    reach = max(haversine_km(lat, lon, h["lat"], h["lon"]) for h in hits)
    return {
        "name": ", ".join(h["name"] for h in hits), "lat": round(lat, 5), "lon": round(lon, 5),
        "radius_km": round(max(FOCUS_RADIUS_KM, reach + FOCUS_RADIUS_KM / 2), 1),
    }


# --- data ----------------------------------------------------------------------------------------

@node("load_area", "Load city boundary, grid and terrain")
def load_area(state: FloodState) -> dict:
    boundary, boundary_fetch = osm.city_boundary()
    cells = build_grid(boundary, settings.bbox, settings.grid_step_deg)
    samples = [p for cell in cells for p in cell.sample_points()]
    heights, elevation_fetches = openmeteo.elevation(samples)
    n = ELEVATION_SAMPLES_PER_CELL
    per_cell = [sum(heights[i * n : (i + 1) * n]) / n for i in range(len(cells))]
    emit(
        "tool_result",
        f"{len(cells)} cells of ~{settings.grid_step_deg * 111:.1f} km cover Chennai; "
        f"mean cell elevation ranges {min(per_cell):.0f}-{max(per_cell):.0f} m",
    )
    return {
        "boundary": boundary,
        "cells": cells,
        "elevations": per_cell,
        "sources": [boundary_fetch.provenance(), elevation_fetches[-1].provenance()],
    }


@node("fetch_rainfall", "Fetch hourly rainfall")
def fetch_rainfall(state: FloodState) -> dict:
    cells = state["cells"]
    rain = openmeteo.rainfall(
        [(c.lat, c.lon) for c in cells],
        horizon_hours=state["horizon_hours"], replay_date=state["replay_date"],
    )
    if len(rain.times) < state["horizon_hours"]:
        emit("warning", f"Only {len(rain.times)} of {state['horizon_hours']} hours are available")
    wettest = max(max_rolling_sum(s, 24) for s in rain.per_point)
    emit(
        "tool_result",
        f"{len(cells)} hourly series, {rain.times[0]} to {rain.times[-1]}; "
        f"wettest cell peaks at {wettest:.0f} mm in 24 h",
    )
    return {"rain": rain, "sources": [rain.fetched.provenance()]}


def climatology_anchors() -> list[tuple[float, float]]:
    """Four points spread over the city; ERA5 cells are ~25 km, so more would repeat the same cell."""
    south, west, north, east = settings.bbox
    return [
        (round(south + (north - south) * fy, 3), round(west + (east - west) * fx, 3))
        for fy in (0.25, 0.75) for fx in (0.25, 0.65)
    ]


@node("fetch_climatology", "Fetch 20-year local rainfall extremes")
def fetch_climatology(state: FloodState) -> dict:
    stats, fetched = openmeteo.climatology(climatology_anchors())
    levels = ", ".join(f"{s.two_year_1d:.0f}" for s in stats)
    emit("tool_result", f"2-year wettest day at {len(stats)} ERA5 anchors over {stats[0].years} years: "
                        f"{levels} mm (record day {max(s.record_1d for s in stats):.0f} mm)")
    return {"climatology": stats, "sources": [fetched.provenance()]}


# --- reasoning -----------------------------------------------------------------------------------

@node("assess_risk", "Score flood risk per cell")
def assess_risk(state: FloodState) -> dict:
    focus = state.get("focus")
    risks = assess(
        state["cells"], state["rain"].per_point, state["elevations"], state["climatology"],
        focus=(focus["lat"], focus["lon"], focus["radius_km"]) if focus else None,
    )
    counts = Counter(r.level for r in risks if r.in_focus)
    emit("reasoning", "Cells by level: " + ", ".join(f"{lvl} {counts.get(lvl, 0)}" for lvl in reversed(LEVELS)),
         counts=dict(counts))

    hazards = sorted((r for r in risks if r.is_hazard and r.in_focus), key=lambda r: -r.score)
    localities: dict[str, str] = {}
    for r in hazards[: settings.max_named_localities]:
        try:
            name, _ = osm.locality_name(r.lat, r.lon)
        except ToolError as exc:
            emit("warning", f"Could not name cell {r.cell_id}: {exc}")
            continue
        if name:
            localities[r.cell_id] = name

    if hazards:
        worst = hazards[0]
        emit(
            "decision",
            f"High risk found: worst is {localities.get(worst.cell_id, worst.cell_id)} "
            f"({worst.level}, {worst.rain_24h_max_mm:.0f} mm in 24 h, {worst.imd_category}). "
            "Checking facilities and routes.",
        )
    else:
        emit("decision", "No cell reaches high risk; skipping facility and route planning.")
    return {"risks": risks, "localities": localities, "outcome": "alert" if hazards else "all_clear"}


def route_after_assessment(state: FloodState) -> str:
    return state["outcome"]


def _cell_id_at(lat: float, lon: float) -> str:
    south, west, _, _ = settings.bbox
    step_deg = settings.grid_step_deg
    return f"r{math.floor((lat - south) / step_deg)}c{math.floor((lon - west) / step_deg)}"


@node("find_facilities", "Find critical facilities in high-risk cells")
def find_facilities(state: FloodState) -> dict:
    try:
        found, fetches, failed = osm.facilities(state["boundary"])
    except ToolError as exc:
        emit("warning", f"Facility lookup failed ({exc}); continuing without facility impact")
        return {"facilities_at_risk": [], "shelters": []}
    for group in failed:
        emit("warning", f"Could not load {group}; those buildings are missing from this run")

    risk_by_cell = {r.cell_id: r for r in state["risks"]}
    localities = state.get("localities", {})
    at_risk, shelters = [], []
    for f in found:
        risk = risk_by_cell.get(_cell_id_at(f.lat, f.lon))
        if risk is None:
            continue
        entry = {
            "id": f.id, "kind": f.kind, "name": f.name, "lat": f.lat, "lon": f.lon,
            "address": f.address, "phone": f.phone,
            "level": risk.level, "score": risk.score, "cell_id": risk.cell_id,
            "locality": localities.get(risk.cell_id),
            "rain_24h_max_mm": risk.rain_24h_max_mm, "rain_total_mm": risk.rain_total_mm,
            "imd_category": risk.imd_category, "elevation_m": risk.elevation_m,
        }
        if risk.is_hazard and risk.in_focus:
            at_risk.append(entry)
        if f.kind in RELIEF_KINDS:
            shelters.append(entry)  # every potential relief site; plan_routes picks the safest

    at_risk.sort(key=lambda f: (KIND_PRIORITY[f["kind"]], f["name"].startswith("Unnamed"), -f["score"]))
    kinds = Counter(f["kind"] for f in at_risk)
    safe = sum(s["level"] in ("low", "moderate") for s in shelters)
    breakdown = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(kinds.items(), key=lambda kv: KIND_PRIORITY[kv[0]]))
    emit(
        "tool_result",
        f"{len(found)} critical buildings mapped; {len(at_risk)} in high-risk cells ({breakdown or 'none'}). "
        f"{safe} relief sites in lower-risk cells.",
        counts=dict(kinds),
    )
    return {"facilities_at_risk": at_risk, "shelters": shelters, "sources": [f.provenance() for f in fetches]}


def _relief_sites(shelters: list[dict], risks) -> tuple[list[dict], float | None]:
    """Relief sites outside high-risk cells; if there are none, the least-affected fifth of the city.

    Returns the sites and, in the fallback case, the score cut-off that defines "least affected".
    """
    safe = [s for s in shelters if s["level"] in ("low", "moderate")]
    if safe:
        return safe, None
    scores = sorted(r.score for r in risks)
    cutoff = scores[max(0, len(scores) // 5 - 1)]
    return [s for s in shelters if s["score"] <= cutoff], cutoff


def hazard_cells_for(cells, risks, cutoff: float | None) -> list[routing.HazardCell]:
    """Cells routes should avoid; with a fallback cut-off, only cells worse than the relief sites' cells."""
    return [
        routing.HazardCell(cell.polygon, risk.level)
        for cell, risk in zip(cells, risks)
        if risk.is_hazard and (cutoff is None or risk.score > cutoff)
    ]


@node("plan_routes", "Plan evacuation routes around risk zones")
def plan_routes(state: FloodState) -> dict:
    at_risk, risks = state.get("facilities_at_risk", []), state["risks"]
    if not at_risk:
        emit("reasoning", "No mapped facilities in high-risk cells; no routes needed")
        return {"routes": []}

    sites, cutoff = _relief_sites(state.get("shelters", []), risks)
    if cutoff is not None:
        emit("reasoning", f"Every cell is high risk, so relief sites are taken from the least-affected "
                          f"fifth of the city (score <= {cutoff:.2f}) and only worse cells are avoided")
    if not sites:
        emit("warning", "No relief site found; recommend sheltering on upper floors instead of evacuation")
        return {"routes": []}

    hazard_cells = hazard_cells_for(state["cells"], risks, cutoff)
    routes = []
    for fac in at_risk[: settings.max_routes]:
        def distance(site: dict) -> float:
            return haversine_km(fac["lat"], fac["lon"], site["lat"], site["lon"])

        # Named sites first: a route to "Unnamed school" is not actionable for a field team.
        candidates = sorted(
            (s for s in sites if distance(s) >= MIN_EVACUATION_KM),
            key=lambda s: (s["name"].startswith("Unnamed"), distance(s)),
        )[:2]
        for dest in candidates:
            try:
                route = routing.plan_route((fac["lat"], fac["lon"]), (dest["lat"], dest["lon"]), hazard_cells)
            except (ToolError, KeyError, IndexError) as exc:
                emit("warning", f"No route {fac['name']} -> {dest['name']}: {exc}")
                continue
            note = "avoids high-risk cells" if route.hazard_overlap_km == 0 else \
                f"still crosses {route.hazard_overlap_km:.1f} km of high-risk cells"
            blocked = f" after blocking {route.blocked_points} road points" if route.blocked_points else ""
            emit("tool_result", f"{fac['name']} -> {dest['name']}: {route.distance_km:.1f} km, "
                                f"{route.duration_min:.0f} min via {route.provider}{blocked}, {note}")
            routes.append({
                "from": {"name": fac["name"], "kind": fac["kind"], "lat": fac["lat"], "lon": fac["lon"]},
                "to": {"name": dest["name"], "kind": dest["kind"], "lat": dest["lat"], "lon": dest["lon"]},
                "coords": route.coords,
                "distance_km": round(route.distance_km, 1),
                "duration_min": round(route.duration_min),
                "provider": route.provider,
                "hazard_overlap_km": route.hazard_overlap_km,
                "blocked_points": route.blocked_points,
                "attempts": route.attempts,
            })
            break
    return {"routes": routes}


# --- communication -------------------------------------------------------------------------------

ALERT_SYSTEM = """You write public flood alerts for residents of Greater Chennai.
Use ONLY the facts in the user message. Do not add any number, place, date, time or phone number
that is not in the facts, and copy numbers exactly as given (no rounding or unit conversion).
Write calm, plain language a resident can act on: name the worst-affected localities, the expected
rainfall in mm with its IMD category, what to do, and the helpline numbers.
Mention evacuation routes only as given in the facts.
Write in English only; a dedicated Indic translation model produces the Tamil version.
Reply with JSON keys:
  headline_en (at most 12 words), body_en (at most 90 words), actions_en (3 to 5 short strings)."""


def _facts(state: FloodState) -> dict:
    risks, rain, focus = state["risks"], state["rain"], state.get("focus")
    localities = state.get("localities", {})
    hazards = sorted((r for r in risks if r.is_hazard and r.in_focus), key=lambda r: -r.score)
    counts = Counter(r.level for r in risks if r.in_focus)
    replay = state.get("replay_date")
    return {
        "city": "Chennai",
        "data_mode": f"replay of archived forecasts starting {replay}" if replay else "live forecast",
        "forecast_window": {
            "start": rain.times[0].replace("T", " "),
            "end": rain.times[-1].replace("T", " "),
            "hours": state["horizon_hours"],
        },
        "focus_area": focus["name"] if focus else "whole city",
        "worst_areas": [
            {
                "locality": localities.get(r.cell_id, "unnamed area"),
                "risk_level": r.level,
                "max_24h_rain_mm": round(r.rain_24h_max_mm),
                "window_total_rain_mm": round(r.rain_total_mm),
                "imd_category": r.imd_category,
            }
            for r in hazards[:5]
        ],
        "grid_cells_by_risk_level": {lvl: counts.get(lvl, 0) for lvl in LEVELS},
        "critical_facilities_in_high_risk_areas": dict(Counter(f["kind"] for f in state.get("facilities_at_risk", []))),
        "evacuation_routes": [
            {
                "from": r["from"]["name"], "to": r["to"]["name"],
                "distance_km": r["distance_km"], "duration_min": r["duration_min"],
                "avoids_high_risk_areas": r["hazard_overlap_km"] == 0,
            }
            for r in state.get("routes", [])
        ],
        "helplines": HELPLINES,
    }


def _template_alert(facts: dict) -> dict:
    worst = facts["worst_areas"][0]
    named = list(dict.fromkeys(a["locality"] for a in facts["worst_areas"] if a["locality"] != "unnamed area"))
    window = facts["forecast_window"]
    lines = [f"{name}: {number}" for name, number in HELPLINES.items()]
    return {
        "headline_en": f"Flood warning for {', '.join(named[:3]) or 'parts of Chennai'}",
        "body_en": (
            f"{worst['imd_category'].capitalize()} rain of up to {worst['max_24h_rain_mm']} mm in 24 hours "
            f"is expected between {window['start']} and {window['end']}. "
            f"Highest flood risk: {', '.join(named) or 'several low-lying areas'}."
        ),
        "actions_en": [
            "Move vehicles, documents and valuables to higher ground.",
            "Avoid waterlogged roads, subways and underpasses.",
            "Keep phones charged and follow official updates.",
            "Helplines - " + "; ".join(lines),
        ],
        "generated_by": "template",
        "guardrail": {"passed": True, "attempts": 0, "note": "deterministic template"},
    }


@node("draft_alert", "Draft English alert and fact-check it")
def draft_alert(state: FloodState) -> dict:
    facts = _facts(state)
    feedback = ""
    for attempt in range(1, 4):
        try:
            alert, model = llm.chat_json(ALERT_SYSTEM, json.dumps(facts, ensure_ascii=False) + feedback)
        except llm.LLMUnavailable as exc:
            emit("warning", f"No LLM available ({exc})")
            break
        problems = check_alert(alert, facts)
        if not problems:
            emit("guardrail", f"Draft {attempt} from {model} passed the fact check")
            alert.update(generated_by=model, guardrail={"passed": True, "attempts": attempt})
            return {"alert": alert, "facts": facts}
        emit("guardrail", f"Draft {attempt} rejected: {'; '.join(problems)}", problems=problems)
        feedback = (
            "\n\nYour previous draft was rejected: " + "; ".join(problems)
            + ". Fix these problems and reply with the complete JSON again."
        )

    emit("warning", "Falling back to the deterministic English template")
    return {"alert": _template_alert(facts), "facts": facts}


def _translate_checked(english: str) -> tuple[str | None, list[str]]:
    try:
        tamil = bodhan.translate(english)
    except (ToolError, KeyError, IndexError, ValueError) as exc:
        return None, [str(exc)]
    problems = check_translation(english, tamil)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", english) if s.strip()]
    if problems and len(sentences) > 1:
        # Long passages sometimes lose a figure; shorter inputs keep them more reliably.
        emit("retry", f"Translation check failed ({'; '.join(problems)}); retrying sentence by sentence")
        try:
            tamil = " ".join(bodhan.translate(s) for s in sentences)
        except (ToolError, KeyError, IndexError, ValueError) as exc:
            return None, problems + [str(exc)]
        problems = check_translation(english, tamil)
    return (None if problems else tamil), problems


@node("translate_alert", "Translate alert to Tamil (Bodhan Indic-Translate) and check it")
def translate_alert(state: FloodState) -> dict:
    alert = dict(state["alert"])
    if not bodhan.translation_configured():
        emit("warning", "BODHAN_TRANSLATE_API_KEY not set; the alert stays English only")
        alert["translation"] = {"provider": None, "passed": False, "problems": ["not configured"]}
        return {"alert": alert}

    fields = [("headline_ta", alert["headline_en"]), ("body_ta", alert["body_en"])]
    fields += [(f"action_{i}", text) for i, text in enumerate(alert["actions_en"])]
    # Fields are independent, so translate a few at once; each worker carries the run's trace context.
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(contextvars.copy_context().run, _translate_checked, text) for _, text in fields]
        results = [f.result() for f in futures]

    problems = [f"{name}: {p}" for (name, _), (_, errs) in zip(fields, results) for p in errs]
    if problems:
        emit("guardrail", f"Tamil translation rejected: {'; '.join(problems)}", problems=problems)
        emit("warning", "Publishing English only rather than an unchecked translation")
        alert["translation"] = {"provider": "Bodhan Indic-Translate", "passed": False, "problems": problems}
        return {"alert": alert}

    tamil = {name: text for (name, _), (text, _) in zip(fields, results)}
    alert.update(
        headline_ta=tamil["headline_ta"],
        body_ta=tamil["body_ta"],
        actions_ta=[tamil[f"action_{i}"] for i in range(len(alert["actions_en"]))],
        translation={"provider": "Bodhan Indic-Translate", "passed": True, "problems": []},
    )
    emit("guardrail", f"Tamil translation of {len(fields)} parts passed: Tamil script, all numbers preserved")
    return {"alert": alert}


@node("all_clear", "Report all clear")
def all_clear(state: FloodState) -> dict:
    risks, focus = state["risks"], state.get("focus")
    wettest = max([r for r in risks if r.in_focus] or risks, key=lambda r: r.rain_24h_max_mm)
    area = focus["name"] if focus else "Chennai"
    summary = (
        f"No part of {area} reaches high flood risk in the next {state['horizon_hours']} h. "
        f"Wettest 24 h: {wettest.rain_24h_max_mm:.0f} mm ({wettest.imd_category})."
    )
    emit("decision", summary)
    return {"alert": None, "facts": {"summary": summary}}
