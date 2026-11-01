"""Grid construction and small geometry helpers (lat/lon degrees in, km out)."""

import math
from dataclasses import dataclass

from shapely import affinity
from shapely.geometry import Point, box
from shapely.geometry.base import BaseGeometry

KM_PER_DEG_LAT = 111.32


@dataclass(frozen=True)
class Cell:
    id: str
    lat: float
    lon: float
    polygon: BaseGeometry

    def sample_points(self) -> list[tuple[float, float]]:
        """Centre plus four inner points, used to average elevation over the cell."""
        minx, miny, maxx, maxy = self.polygon.bounds
        qx, qy = (maxx - minx) / 4, (maxy - miny) / 4
        return [
            (self.lat, self.lon),
            (miny + qy, minx + qx), (miny + qy, maxx - qx),
            (maxy - qy, minx + qx), (maxy - qy, maxx - qx),
        ]


def build_grid(boundary: BaseGeometry, bbox: tuple[float, float, float, float], step: float) -> list[Cell]:
    """Square cells covering the boundary; a cell is kept when at least 30% of it is inside."""
    south, west, north, east = bbox
    cells = []
    rows = math.ceil((north - south) / step)
    cols = math.ceil((east - west) / step)
    for r in range(rows):
        for c in range(cols):
            poly = box(west + c * step, south + r * step, west + (c + 1) * step, south + (r + 1) * step)
            if poly.intersection(boundary).area / poly.area >= 0.3:
                centre = poly.centroid
                cells.append(Cell(f"r{r}c{c}", round(centre.y, 5), round(centre.x, 5), poly))
    return cells


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _km_per_deg_lon(lat: float) -> float:
    return KM_PER_DEG_LAT * math.cos(math.radians(lat))


def length_km(geom: BaseGeometry) -> float:
    if geom.is_empty:
        return 0.0
    lat = geom.centroid.y
    # Scale degrees to km on both axes before measuring so diagonal segments are not distorted.
    scaled = affinity.scale(geom, xfact=_km_per_deg_lon(lat), yfact=KM_PER_DEG_LAT, origin=(0, 0))
    return scaled.length


def buffer_km(lat: float, lon: float, km: float) -> BaseGeometry:
    """Approximate circle of `km` radius around a point (ellipse in degree space)."""
    circle = Point(lon, lat).buffer(km / KM_PER_DEG_LAT, 24)
    return affinity.scale(circle, xfact=KM_PER_DEG_LAT / _km_per_deg_lon(lat), yfact=1.0)
