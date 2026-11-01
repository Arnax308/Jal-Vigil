"""On-demand safe route for one person: from where they are to the nearest relief site.

Uses the hazard cells and relief sites of a finished run, so the answer matches the map the officer
is looking at. Runs outside the graph because it is a question asked after the assessment.
"""

from typing import Any

from ..clients import routing
from ..geo import haversine_km
from ..http import ToolError
from .nodes import _cell_id_at, _relief_sites, hazard_cells_for

CANDIDATES = 2     # relief sites routed to; the better route wins
NEAREST_POOL = 6   # nearest sites considered, so a named site can win over a slightly closer unnamed one


def plan(state: dict[str, Any], lat: float, lon: float, label: str) -> dict[str, Any]:
    risks = state["risks"]
    risk_by_cell = {r.cell_id: r for r in risks}
    here = risk_by_cell.get(_cell_id_at(lat, lon))
    sites, cutoff = _relief_sites(state.get("shelters", []), risks)
    if not sites:
        raise LookupError("No relief site is known for this run; shelter on an upper floor instead")

    def distance(site: dict) -> float:
        return haversine_km(lat, lon, site["lat"], site["lon"])

    nearest = sorted(sites, key=distance)[:NEAREST_POOL]
    candidates = sorted(nearest, key=lambda s: (s["name"].startswith("Unnamed"), distance(s)))[:CANDIDATES]
    hazards = hazard_cells_for(state["cells"], risks, cutoff)

    best, errors = None, []
    for site in candidates:
        try:
            route = routing.plan_route((lat, lon), (site["lat"], site["lon"]), hazards)
        except (ToolError, KeyError, IndexError) as exc:
            errors.append(f"{site['name']}: {exc}")
            continue
        key = (route.hazard_overlap_km, route.duration_min)
        if best is None or key < best[0]:
            best = (key, site, route)
        if route.hazard_overlap_km == 0:
            break
    if best is None:
        raise ToolError("Routing", "; ".join(errors) or "no route found")

    _, site, route = best
    return {
        "from": {
            "name": label, "lat": lat, "lon": lon,
            "level": here.level if here else None,
            "rain_24h_max_mm": here.rain_24h_max_mm if here else None,
        },
        "to": {k: site[k] for k in ("name", "kind", "lat", "lon", "address", "phone", "level", "locality")},
        "coords": route.coords,
        "distance_km": round(route.distance_km, 1),
        "duration_min": round(route.duration_min),
        "provider": route.provider,
        "hazard_overlap_km": route.hazard_overlap_km,
        "blocked_points": route.blocked_points,
        "attempts": route.attempts,
        "sites_considered": [s["name"] for s in candidates],
    }
