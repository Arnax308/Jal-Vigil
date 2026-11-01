"""Open-Meteo: rainfall forecasts, archived forecasts (replay), elevation and rainfall climatology."""

import hashlib
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np

from ..config import settings
from ..events import emit
from ..http import Fetched, fetch_json

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"

CLIMATOLOGY_START, CLIMATOLOGY_END = "2005-01-01", "2024-12-31"
# Earliest day the historical-forecast archive covers.
REPLAY_EARLIEST = date(2022, 1, 1)
# India has no daylight saving, so IST is always UTC+05:30. A fixed offset avoids zoneinfo, which
# needs the tzdata package on Windows (not installed in a fresh virtual environment).
IST = timezone(timedelta(hours=5, minutes=30), "IST")


@dataclass(frozen=True)
class RainSeries:
    times: list[str]               # local ISO hours, Asia/Kolkata
    per_point: list[list[float]]   # mm per hour, aligned with `times`
    missing_ratio: float
    fetched: Fetched


@dataclass(frozen=True)
class Climatology:
    lat: float
    lon: float
    two_year_1d: float  # median of each year's wettest day (~2-year return level)
    two_year_3d: float  # same for the wettest 3-day total
    record_1d: float    # wettest single day in the period
    years: int


def _coords_key(points: list[tuple[float, float]]) -> str:
    raw = ";".join(f"{lat:.4f},{lon:.4f}" for lat, lon in points)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _coord_params(points: list[tuple[float, float]]) -> dict[str, str]:
    return {
        "latitude": ",".join(f"{lat:.4f}" for lat, _ in points),
        "longitude": ",".join(f"{lon:.4f}" for _, lon in points),
    }


def _as_list(payload):
    return payload if isinstance(payload, list) else [payload]


def rainfall(points: list[tuple[float, float]], *, horizon_hours: int, replay_date: date | None) -> RainSeries:
    """Hourly precipitation for each point over the analysis window.

    Live mode starts at the current hour. Replay mode starts at 00:00 on `replay_date` and reads
    Open-Meteo's archive of past model runs, so the agent sees what forecasters had at the time.
    """
    params = {**_coord_params(points), "hourly": "precipitation", "timezone": settings.timezone}
    key = _coords_key(points)
    if replay_date is None:
        params["forecast_days"] = min(16, math.ceil(horizon_hours / 24) + 1)
        fetched = fetch_json(
            "Open-Meteo Forecast API", [FORECAST_URL], params=params,
            cache_key=f"forecast:{key}:{params['forecast_days']}", ttl=15 * 60,
        )
        now = datetime.now(IST).strftime("%Y-%m-%dT%H:00")
    else:
        end = replay_date + timedelta(days=math.ceil(horizon_hours / 24) - 1)
        params.update(start_date=replay_date.isoformat(), end_date=end.isoformat())
        fetched = fetch_json(
            "Open-Meteo Historical Forecast API", [HISTORICAL_FORECAST_URL], params=params,
            cache_key=f"replay:{key}:{replay_date}:{end}",
        )
        now = f"{replay_date.isoformat()}T00:00"

    locations = _as_list(fetched.data)
    times = locations[0]["hourly"]["time"]
    start = next((i for i, t in enumerate(times) if t >= now), 0)
    window = slice(start, start + horizon_hours)

    per_point, missing, total = [], 0, 0
    for loc in locations:
        values = loc["hourly"]["precipitation"][window]
        missing += sum(v is None for v in values)
        total += len(values)
        per_point.append([float(v) if v is not None else 0.0 for v in values])
    ratio = missing / total if total else 1.0
    if ratio > 0:
        emit("warning", f"{ratio:.0%} of hourly rainfall values were missing and counted as 0 mm")
    return RainSeries(times[window], per_point, ratio, fetched)


def elevation(points: list[tuple[float, float]]) -> tuple[list[float], list[Fetched]]:
    """Copernicus DEM (90 m) elevation in metres; the API takes at most 100 points per call."""
    heights, fetches = [], []
    for i in range(0, len(points), 100):
        chunk = points[i : i + 100]
        fetched = fetch_json(
            "Open-Meteo Elevation API (Copernicus DEM)", [ELEVATION_URL],
            params=_coord_params(chunk), cache_key=f"elevation:{_coords_key(chunk)}",
        )
        heights.extend(float(h) for h in fetched.data["elevation"])
        fetches.append(fetched)
    return heights, fetches


def climatology(anchors: list[tuple[float, float]]) -> tuple[list[Climatology], Fetched]:
    """Daily rainfall statistics from the ERA5 reanalysis, one set per anchor point.

    ERA5 cells are ~25 km, so a handful of anchors covers Chennai; asking for every grid cell
    would only repeat the same reanalysis cell and burn the API quota.
    """
    params = {
        **_coord_params(anchors),
        "start_date": CLIMATOLOGY_START,
        "end_date": CLIMATOLOGY_END,
        "daily": "precipitation_sum",
        "timezone": settings.timezone,
    }
    fetched = fetch_json(
        "Open-Meteo Historical Weather API (ERA5)", [ARCHIVE_URL], params=params,
        cache_key=f"climatology:{_coords_key(anchors)}:{CLIMATOLOGY_START}:{CLIMATOLOGY_END}",
        timeout=60,
    )
    stats = []
    for (lat, lon), loc in zip(anchors, _as_list(fetched.data)):
        daily = np.nan_to_num(np.array([np.nan if v is None else v for v in loc["daily"]["precipitation_sum"]], dtype=float))
        three_day = np.concatenate([daily[:2], np.convolve(daily, np.ones(3), mode="valid")])
        years = np.array([int(t[:4]) for t in loc["daily"]["time"]])
        annual_max_1d = [daily[years == y].max() for y in np.unique(years)]
        annual_max_3d = [three_day[years == y].max() for y in np.unique(years)]
        stats.append(
            Climatology(
                lat=lat, lon=lon,
                two_year_1d=float(np.median(annual_max_1d)),
                two_year_3d=float(np.median(annual_max_3d)),
                record_1d=float(daily.max()),
                years=len(annual_max_1d),
            )
        )
    return stats, fetched
