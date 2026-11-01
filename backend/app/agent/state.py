import operator
from datetime import date
from typing import Annotated, Any, TypedDict

from shapely.geometry.base import BaseGeometry

from ..clients.openmeteo import Climatology, RainSeries
from ..geo import Cell
from ..risk import CellRisk


class FloodState(TypedDict, total=False):
    request: dict[str, Any]
    horizon_hours: int
    replay_date: date | None
    focus: dict[str, Any] | None  # name, lat, lon, radius_km

    boundary: BaseGeometry
    cells: list[Cell]
    elevations: list[float]
    rain: RainSeries
    climatology: list[Climatology]

    risks: list[CellRisk]
    localities: dict[str, str]  # cell id -> place name
    outcome: str  # alert | all_clear

    facilities_at_risk: list[dict[str, Any]]
    shelters: list[dict[str, Any]]  # every school / shelter with its cell's level and score
    routes: list[dict[str, Any]]
    facts: dict[str, Any]
    alert: dict[str, Any]

    # Reducer so parallel nodes can both report the sources they used.
    sources: Annotated[list[dict[str, Any]], operator.add]
