"""Fill the response cache in the background when the server starts.

On a fresh host (a new container, a wiped disk) every source is fetched live once, and the slowest,
Overpass, can take minutes when its public servers are busy. Doing that at boot means it is usually
done before anyone presses Deploy. Runs in a daemon thread and never blocks or crashes the server.
"""

import logging
import threading
import time

from .agent.nodes import climatology_anchors
from .clients import openmeteo, osm
from .config import settings
from .geo import build_grid

log = logging.getLogger("uvicorn.error")


BUILDING_ATTEMPTS = 5
RETRY_PAUSE_S = 30


def warm_cache() -> None:
    started = time.time()
    try:
        boundary, _ = osm.city_boundary()
        cells = build_grid(boundary, settings.bbox, settings.grid_step_deg)
        openmeteo.elevation([p for cell in cells for p in cell.sample_points()])
        openmeteo.climatology(climatology_anchors())
        # Overpass is often overloaded; groups already cached return at once, so retrying is cheap.
        for attempt in range(1, BUILDING_ATTEMPTS + 1):
            found, _, failed = osm.facilities(boundary)
            if not failed or attempt == BUILDING_ATTEMPTS:
                break
            log.info("Cache warm-up: %s not loaded yet, retrying in %ds", ", ".join(f.split(" (")[0] for f in failed),
                     RETRY_PAUSE_S)
            time.sleep(RETRY_PAUSE_S)
    except Exception as exc:  # warm-up is best effort; the first run fetches whatever is missing
        log.warning("Cache warm-up stopped after %.0fs: %s", time.time() - started, exc)
        return
    note = f"; still missing: {', '.join(failed)}" if failed else ""
    log.info("Cache warm-up done in %.0fs: %d buildings%s", time.time() - started, len(found), note)


def start() -> None:
    if settings.warm_cache:
        threading.Thread(target=warm_cache, name="cache-warmup", daemon=True).start()
