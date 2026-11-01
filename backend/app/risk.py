"""Per-cell flood risk from forecast rainfall, local rainfall extremes and terrain.

score = rain_index * terrain_factor
  rain_index     = max(peak 24 h rain / local 2-year wettest day,
                       window total / local 2-year wettest 3 days)
  terrain_factor = 0.7 .. 1.3, higher for the lowest-lying cells in the city

The level starts from the score band and is then bounded by the IMD 24-hour rainfall category:
an officially "very heavy" day is never reported below high, and rain below IMD "heavy" is never
reported above moderate unless the multi-day total alone exceeds the 2-year level.
"""

from dataclasses import dataclass

import numpy as np

from .clients.openmeteo import Climatology
from .geo import Cell, haversine_km

LEVELS = ("low", "moderate", "high", "severe")
SCORE_BANDS = ((1.6, "severe"), (1.0, "high"), (0.5, "moderate"))

# IMD 24-hour rainfall categories (mm).
IMD_CATEGORIES = (
    (204.5, "extremely heavy"),
    (115.6, "very heavy"),
    (64.5, "heavy"),
    (15.6, "moderate"),
    (2.5, "light"),
)
IMD_LEVEL_FLOOR = {"extremely heavy": "severe", "very heavy": "high", "heavy": "moderate"}
IMD_HEAVY_MM = 64.5


@dataclass(frozen=True)
class CellRisk:
    cell_id: str
    lat: float
    lon: float
    rain_24h_max_mm: float
    rain_total_mm: float
    peak_hourly_mm: float
    elevation_m: float
    lowness: float
    rain_index: float
    score: float
    level: str
    imd_category: str
    in_focus: bool

    @property
    def is_hazard(self) -> bool:
        return self.level in ("high", "severe")


def imd_category(mm_24h: float) -> str:
    for threshold, label in IMD_CATEGORIES:
        if mm_24h >= threshold:
            return label
    return "very light / none"


def _band(score: float) -> str:
    for threshold, label in SCORE_BANDS:
        if score >= threshold:
            return label
    return "low"


def _max_level(*levels: str) -> str:
    return max(levels, key=LEVELS.index)


def _min_level(*levels: str) -> str:
    return min(levels, key=LEVELS.index)


def max_rolling_sum(series: list[float], hours: int) -> float:
    values = np.asarray(series, dtype=float)
    if len(values) <= hours:
        return float(values.sum())
    return float(np.convolve(values, np.ones(hours), mode="valid").max())


def _lowness(elevations: list[float]) -> list[float]:
    """0 for the highest cell in the city, 1 for the lowest (rank-based, robust to DEM noise)."""
    if len(elevations) < 2:
        return [0.5] * len(elevations)
    ranks = np.argsort(np.argsort(elevations))
    return [1 - r / (len(elevations) - 1) for r in ranks]


def _nearest(cell: Cell, climatologies: list[Climatology]) -> Climatology:
    return min(climatologies, key=lambda c: haversine_km(cell.lat, cell.lon, c.lat, c.lon))


def assess(
    cells: list[Cell],
    rain_per_cell: list[list[float]],
    elevations: list[float],
    climatologies: list[Climatology],
    focus: tuple[float, float, float] | None = None,  # lat, lon, radius_km
) -> list[CellRisk]:
    risks = []
    for cell, series, elev, low in zip(cells, rain_per_cell, elevations, _lowness(elevations)):
        clim = _nearest(cell, climatologies)
        r24 = max_rolling_sum(series, 24)
        total = float(sum(series))
        multi_day_index = total / max(clim.two_year_3d, 1.0)
        rain_index = max(r24 / max(clim.two_year_1d, 1.0), multi_day_index)
        score = rain_index * (0.7 + 0.6 * low)
        category = imd_category(r24)
        level = _band(score)
        if r24 < IMD_HEAVY_MM and multi_day_index < 1.0:
            level = _min_level(level, "moderate")
        level = _max_level(level, IMD_LEVEL_FLOOR.get(category, "low"))
        in_focus = focus is None or haversine_km(cell.lat, cell.lon, focus[0], focus[1]) <= focus[2]
        risks.append(
            CellRisk(
                cell_id=cell.id, lat=cell.lat, lon=cell.lon,
                rain_24h_max_mm=round(r24, 1), rain_total_mm=round(total, 1),
                peak_hourly_mm=round(max(series, default=0.0), 1),
                elevation_m=round(elev, 1), lowness=round(low, 2),
                rain_index=round(rain_index, 2), score=round(score, 2),
                level=level, imd_category=category, in_focus=in_focus,
            )
        )
    return risks
