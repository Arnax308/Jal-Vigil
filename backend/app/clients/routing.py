"""Driving routes that steer around high-risk cells by blocking the roads they would use.

The loop, on FOSSGIS's public Valhalla server (no key):
  1. route normally
  2. find the stretch of the route that runs through risk cells
  3. block road points on that stretch (`exclude_locations`) and route again
  4. repeat until the route is clear, the server's 50-point limit is reached, or no path is left
The best route seen (least km in risk cells, then shortest) is returned. If Valhalla is down, OSRM
gives the plain shortest route and the overlap is reported instead of hidden.
"""

import hashlib
import json
from dataclasses import dataclass, field

from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.prepared import prep

from ..events import emit
from ..geo import buffer_km, length_km
from ..http import Throttle, ToolError, fetch_json

VALHALLA_URL = "https://valhalla1.openstreetmap.de/route"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"

MAX_EXCLUDES = 50        # FOSSGIS server limit on exclude_locations
POINTS_PER_ROUND = 12
MAX_ROUNDS = 3           # reroutes after the first attempt
ENDPOINT_CLEARANCE_KM = 0.6  # trips start inside a risk cell, so the first/last stretch is unavoidable
ROUTE_CACHE_TTL = 24 * 3600

_valhalla_throttle = Throttle(1.1)  # public server fair use: about one request per second


@dataclass
class HazardCell:
    polygon: BaseGeometry
    level: str  # high | severe


@dataclass
class Route:
    coords: list[tuple[float, float]]  # (lat, lon)
    distance_km: float
    duration_min: float
    provider: str
    hazard_overlap_km: float = 0.0
    blocked_points: int = 0
    attempts: list[str] = field(default_factory=list)


def _decode_polyline6(shape: str) -> list[tuple[float, float]]:
    coords, index, lat, lon = [], 0, 0, 0
    while index < len(shape):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                byte = ord(shape[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat, lon = lat + deltas[0], lon + deltas[1]
        coords.append((lat / 1e6, lon / 1e6))
    return coords


def _valhalla(origin, dest, excludes: list[tuple[float, float]]) -> Route:
    body = {
        "locations": [{"lat": origin[0], "lon": origin[1]}, {"lat": dest[0], "lon": dest[1]}],
        "costing": "auto",
        "directions_options": {"units": "kilometers"},
    }
    if excludes:
        body["exclude_locations"] = [{"lat": lat, "lon": lon} for lat, lon in excludes]
    key = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:20]
    fetched = fetch_json(
        "Valhalla routing (FOSSGIS)", [VALHALLA_URL], method="POST", json_body=body,
        cache_key=f"valhalla:{key}", ttl=ROUTE_CACHE_TTL, retries=1, throttle=_valhalla_throttle,
    )
    trip = fetched.data["trip"]
    coords = [c for leg in trip["legs"] for c in _decode_polyline6(leg["shape"])]
    return Route(coords, trip["summary"]["length"], trip["summary"]["time"] / 60, "Valhalla", blocked_points=len(excludes))


def _osrm(origin, dest) -> Route:
    url = OSRM_URL.format(lat1=origin[0], lon1=origin[1], lat2=dest[0], lon2=dest[1])
    fetched = fetch_json("OSRM public router", [url], params={"overview": "full", "geometries": "geojson"})
    route = fetched.data["routes"][0]
    coords = [(lat, lon) for lon, lat in route["geometry"]["coordinates"]]
    return Route(coords, route["distance"] / 1000, route["duration"] / 60, "OSRM")


def _risk_stretch(route: Route, zone: BaseGeometry | None) -> tuple[float, list[tuple[float, float]]]:
    """Km of the route inside the risk zone, and the route points that lie inside it."""
    if zone is None or zone.is_empty or len(route.coords) < 2:
        return 0.0, []
    line = LineString([(lon, lat) for lat, lon in route.coords])
    overlap = round(length_km(line.intersection(zone)), 2)
    inside = prep(zone)
    points = [(lat, lon) for lat, lon in route.coords if inside.contains(Point(lon, lat))]
    return overlap, points


def _spread(points: list[tuple[float, float]], count: int) -> list[tuple[float, float]]:
    if len(points) <= count:
        return points
    step = len(points) / count
    return [points[int(i * step)] for i in range(count)]


def plan_route(origin: tuple[float, float], dest: tuple[float, float], cells: list[HazardCell]) -> Route:
    hazards = unary_union([c.polygon for c in cells]) if cells else None
    clearance = unary_union([buffer_km(*origin, ENDPOINT_CLEARANCE_KM), buffer_km(*dest, ENDPOINT_CLEARANCE_KM)])
    zone = hazards.difference(clearance) if hazards is not None else None

    attempts: list[str] = []
    best: Route | None = None
    excludes: list[tuple[float, float]] = []
    for round_no in range(MAX_ROUNDS + 1):
        try:
            route = _valhalla(origin, dest, excludes)
        except (ToolError, KeyError, IndexError) as exc:
            attempts.append(f"Valhalla with {len(excludes)} blocked points failed: {exc}")
            if best is None:
                emit("retry", f"Valhalla unavailable ({exc}); falling back to OSRM")
            break
        route.hazard_overlap_km, inside = _risk_stretch(route, zone)
        attempts.append(f"Valhalla, {len(excludes)} blocked points: {route.distance_km:.1f} km, "
                        f"{route.hazard_overlap_km:.1f} km in risk cells")
        if best is None or (route.hazard_overlap_km, route.distance_km) < (best.hazard_overlap_km, best.distance_km):
            best = route
        room = MAX_EXCLUDES - len(excludes)
        if route.hazard_overlap_km == 0 or not inside or room <= 0 or round_no == MAX_ROUNDS:
            break
        new_points = _spread(inside, min(POINTS_PER_ROUND, room))
        emit("retry", f"Route crosses {route.hazard_overlap_km:.1f} km of risk cells; "
                      f"blocking {len(new_points)} road points there and rerouting")
        excludes += new_points

    if best is None:
        best = _osrm(origin, dest)
        best.hazard_overlap_km, _ = _risk_stretch(best, zone)
        attempts.append(f"OSRM shortest route (no avoidance): {best.hazard_overlap_km:.1f} km in risk cells")
    best.attempts = attempts
    return best
