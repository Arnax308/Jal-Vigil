"""Shared HTTP layer: retries, mirror fallback, response caching, stale-if-error and throttling."""

import threading
import time
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from .cache import cache
from .config import settings
from .events import emit

_client = httpx.Client(headers={"User-Agent": settings.user_agent}, follow_redirects=True)


@dataclass(frozen=True)
class Fetched:
    data: Any
    source: str
    fetched_at: float
    from_cache: bool
    stale: bool = False

    def provenance(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "fetched_at": self.fetched_at,
            "from_cache": self.from_cache,
            "stale": self.stale,
        }


class ToolError(Exception):
    def __init__(self, source: str, message: str, status: int | None = None, body: str = ""):
        super().__init__(f"{source}: {message}")
        self.source = source
        self.status = status
        self.body = body


class Throttle:
    """Spaces out live requests to a volunteer-run server (e.g. one per second for Nominatim)."""

    def __init__(self, interval: float):
        self._interval = interval
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            pause = self._interval - (time.time() - self._last)
            if pause > 0:
                time.sleep(pause)
            self._last = time.time()


def _backoff(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying: the server's retry-after if given, longer for rate limits."""
    retry_after = resp.headers.get("retry-after", "")
    if retry_after.isdigit():
        return min(float(retry_after), 30.0)
    return 5.0 * (attempt + 1) if resp.status_code == 429 else 1.5 * (attempt + 1)


def fetch_json(
    source: str,
    urls: Sequence[str],
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    cache_key: str | None = None,
    ttl: float | None = None,
    retries: int = 2,
    timeout: float = 25.0,
    throttle: Throttle | None = None,
) -> Fetched:
    """Fetch JSON from the first URL that answers.

    `ttl=None` with a `cache_key` means the cached copy never expires (static reference data).
    A 4xx other than 429 is raised immediately as ToolError so callers can react to it.
    """
    hit = cache.get(cache_key) if cache_key else None
    if hit and (ttl is None or time.time() - hit.fetched_at < ttl):
        return Fetched(hit.data, source, hit.fetched_at, from_cache=True)

    errors: list[str] = []
    for index, url in enumerate(urls):
        for attempt in range(retries + 1):
            if throttle:
                throttle.wait()
            try:
                resp = _client.request(
                    method, url, params=params, data=data, json=json_body,
                    headers=headers, timeout=timeout,
                )
            except httpx.HTTPError as exc:
                error, wait = f"{type(exc).__name__}", 1.5 * (attempt + 1)
            else:
                if resp.status_code == 200:
                    payload = resp.json()
                    fetched_at = cache.put(cache_key, source, payload) if cache_key else time.time()
                    return Fetched(payload, source, fetched_at, from_cache=False)
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    raise ToolError(source, f"HTTP {resp.status_code}", resp.status_code, resp.text[:500])
                error = f"HTTP {resp.status_code}"
                wait = _backoff(resp, attempt)

            errors.append(f"{httpx.URL(url).host} {error}")
            if attempt < retries:
                emit("retry", f"{source}: {error} from {httpx.URL(url).host}, retrying in {wait:.0f}s")
                time.sleep(wait)
        if index + 1 < len(urls):
            emit("retry", f"{source}: {httpx.URL(url).host} unavailable, switching mirror")

    if hit:
        age_min = (time.time() - hit.fetched_at) / 60
        emit("warning", f"{source}: live fetch failed, using cached copy from {age_min:.0f} min ago")
        return Fetched(hit.data, source, hit.fetched_at, from_cache=True, stale=True)
    raise ToolError(source, "; ".join(errors) or "no response")


def fetch_bytes(
    source: str, url: str, *, json_body: Any, headers: dict[str, str], retries: int = 2, timeout: float = 90.0
) -> bytes:
    """POST and return the raw response body (audio). Callers cache the result on disk themselves."""
    error = "no response"
    for attempt in range(retries + 1):
        try:
            resp = _client.post(url, json=json_body, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            error, wait = type(exc).__name__, 1.5 * (attempt + 1)
        else:
            if resp.status_code == 200:
                return resp.content
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                raise ToolError(source, f"HTTP {resp.status_code}", resp.status_code, resp.text[:500])
            error = f"HTTP {resp.status_code}"
            wait = _backoff(resp, attempt)
        if attempt < retries:
            emit("retry", f"{source}: {error}, retrying in {wait:.0f}s")
            time.sleep(wait)
    raise ToolError(source, error)
