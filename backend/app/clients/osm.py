"""OpenStreetMap: city boundary and place names (Nominatim), critical facilities (Overpass)."""

import contextvars
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry

from ..config import settings
from ..http import Fetched, Throttle, ToolError, fetch_json

NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE = "https://nominatim.openstreetmap.org/reverse"

DAY = 24 * 3600
_nominatim_throttle = Throttle(1.1)  # Nominatim usage policy: at most one request per second


@dataclass(frozen=True)
class Facility:
    id: str
    kind: str  # one of FACILITY_KINDS
    name: str
    lat: float
    lon: float
    address: str = ""
    phone: str = ""


def _nominatim(url: str, params: dict, cache_key: str, ttl: float | None) -> Fetched:
    return fetch_json(
        "OSM Nominatim", [url], params=params, cache_key=cache_key, ttl=ttl, throttle=_nominatim_throttle,
    )


def city_boundary() -> tuple[BaseGeometry, Fetched]:
    fetched = _nominatim(
        NOMINATIM_SEARCH,
        {"q": settings.city_query, "format": "jsonv2", "polygon_geojson": 1,
         "polygon_threshold": 0.0005, "limit": 1},
        cache_key=f"boundary:{settings.city_query}", ttl=30 * DAY,
    )
    if not fetched.data:
        raise ToolError("OSM Nominatim", f"no boundary found for {settings.city_query}")
    return shape(fetched.data[0]["geojson"]), fetched


def geocode_in_city(place: str) -> tuple[dict | None, Fetched]:
    """Find a locality inside the city's bounding box; returns None when nothing matches."""
    south, west, north, east = settings.bbox
    fetched = _nominatim(
        NOMINATIM_SEARCH,
        {"q": place, "format": "jsonv2", "limit": 1, "bounded": 1,
         "viewbox": f"{west},{north},{east},{south}"},
        cache_key=f"geocode:{place.lower().strip()}", ttl=30 * DAY,
    )
    if not fetched.data:
        return None, fetched
    hit = fetched.data[0]
    return {"name": hit.get("name") or place, "lat": float(hit["lat"]), "lon": float(hit["lon"])}, fetched


def locality_name(lat: float, lon: float) -> tuple[str | None, Fetched]:
    fetched = _nominatim(
        NOMINATIM_REVERSE,
        {"lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "format": "jsonv2", "zoom": 15},
        cache_key=f"reverse:{lat:.4f},{lon:.4f}", ttl=None,
    )
    address = fetched.data.get("address", {}) if isinstance(fetched.data, dict) else {}
    for field in ("suburb", "neighbourhood", "quarter", "city_district", "village", "town"):
        if address.get(field):
            return address[field], fetched
    return None, fetched


# Buildings that matter in a flood: where vulnerable people are, where help comes from, where people
# can shelter. Order is the priority used for evacuation planning.
FACILITY_KINDS = ("hospital", "care_home", "clinic", "shelter", "school", "college", "police", "fire_station")

# Fetched as separate queries, each cached on its own: public Overpass servers are often overloaded,
# and one failed group should cost only that group, not every building.
FACILITY_GROUPS = {
    "health": ('"amenity"="hospital"', '"amenity"="clinic"', '"social_facility"="nursing_home"',
               '"social_facility"="group_home"', '"social_facility"="assisted_living"'),
    "education": ('"amenity"="school"', '"amenity"="college"', '"amenity"="university"'),
    "shelter": ('"amenity"="community_centre"', '"social_facility"="shelter"', '"emergency"="assembly_point"'),
    "emergency services": ('"amenity"="police"', '"amenity"="fire_station"'),
}
_FACILITY_QUERY = "[out:json][timeout:60];({statements});out center tags;"


def _kind(tags: dict) -> str:
    amenity, social = tags.get("amenity"), tags.get("social_facility")
    if amenity in ("hospital", "clinic", "school", "police", "fire_station"):
        return amenity
    if amenity in ("college", "university"):
        return "college"
    if social in ("nursing_home", "group_home", "assisted_living"):
        return "care_home"
    return "shelter"


def _address(tags: dict) -> str:
    if tags.get("addr:full"):
        return tags["addr:full"]
    street = " ".join(filter(None, (tags.get("addr:housenumber"), tags.get("addr:street"))))
    parts = (street, tags.get("addr:suburb"), tags.get("addr:city"), tags.get("addr:postcode"))
    return ", ".join(p for p in parts if p)


def _fetch_group(index: int, group: str, filters: tuple[str, ...]) -> Fetched:
    s, w, n, e = settings.bbox
    statements = "".join(f"nwr[{f}]({s},{w},{n},{e});" for f in filters)
    # Each group starts on a different mirror, so parallel queries don't all queue on one server.
    mirrors = settings.overpass_urls[index:] + settings.overpass_urls[:index]
    return fetch_json(
        "OSM Overpass API", mirrors, method="POST",
        data={"data": _FACILITY_QUERY.format(statements=statements)},
        cache_key=f"facilities:{group}:{s},{w},{n},{e}", ttl=30 * DAY, retries=0, timeout=60,
    )


def facilities(boundary: BaseGeometry) -> tuple[list[Facility], list[Fetched], list[str]]:
    """Critical buildings inside the city; returns them, the fetches used, and any groups that failed.

    The groups are fetched in parallel; each worker carries the caller's trace context.
    """
    found, fetches, failed = [], [], []
    groups = list(FACILITY_GROUPS.items())
    with ThreadPoolExecutor(max_workers=len(groups)) as pool:
        futures = [
            pool.submit(contextvars.copy_context().run, _fetch_group, i, group, filters)
            for i, (group, filters) in enumerate(groups)
        ]
    for (group, _), future in zip(groups, futures):
        try:
            fetched = future.result()
        except ToolError as exc:
            failed.append(f"{group} ({exc})")
            continue
        fetches.append(fetched)
        for el in fetched.data.get("elements", []):
            lat = el.get("lat", el.get("center", {}).get("lat"))
            lon = el.get("lon", el.get("center", {}).get("lon"))
            if lat is None or lon is None or not boundary.contains(Point(lon, lat)):
                continue
            tags = el.get("tags", {})
            kind = _kind(tags)
            name = tags.get("name:en") or tags.get("name") or f"Unnamed {kind.replace('_', ' ')}"
            phone = tags.get("phone") or tags.get("contact:phone") or ""
            found.append(Facility(f"{el['type']}/{el['id']}", kind, name, float(lat), float(lon), _address(tags), phone))
    if not fetches:
        raise ToolError("OSM Overpass API", "; ".join(failed))
    # An element can match two groups (a hospital that is also an assembly point); keep it once.
    unique = {f.id: f for f in found}
    return list(unique.values()), fetches, failed
