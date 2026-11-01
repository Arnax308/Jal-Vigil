"""Per-run event log that the UI streams over SSE.

Nodes and tool clients call `emit(...)`; the run that receives the event is taken from a context
variable, so tool code does not need the run passed through every call.
"""

import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

_current_run: ContextVar["Run | None"] = ContextVar("current_run", default=None)
_current_step: ContextVar[str | None] = ContextVar("current_step", default=None)


@dataclass
class Run:
    id: str
    request: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    status: str = "running"  # running | awaiting_approval | all_clear | approved | failed
    result: dict[str, Any] | None = None
    state: dict[str, Any] | None = None  # final graph state, kept for follow-up questions (safe routes)
    finished: bool = False  # set once background work (the voice alert) is done too; ends the SSE stream
    events: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, type_: str, message: str, step: str | None = None, **data: Any) -> None:
        with self._lock:
            self.events.append(
                {
                    "seq": len(self.events),
                    "ts": time.time(),
                    "type": type_,
                    "step": step,
                    "message": message,
                    "data": data or None,
                }
            )

    def events_since(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return self.events[seq:]


class RunRegistry:
    def __init__(self, keep: int = 25):
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._keep = keep
        self._lock = threading.Lock()

    def create(self, request: dict[str, Any]) -> Run:
        run = Run(id=uuid.uuid4().hex[:12], request=request)
        with self._lock:
            self._runs[run.id] = run
            while len(self._runs) > self._keep:
                self._runs.popitem(last=False)
        return run

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._runs.get(run_id)


registry = RunRegistry()


def emit(type_: str, message: str, **data: Any) -> None:
    run = _current_run.get()
    if run is not None:
        run.add(type_, message, step=_current_step.get(), **data)


@contextmanager
def bind(run: Run) -> Iterator[None]:
    token = _current_run.set(run)
    try:
        yield
    finally:
        _current_run.reset(token)


@contextmanager
def step(name: str, title: str) -> Iterator[None]:
    token = _current_step.set(name)
    started = time.perf_counter()
    emit("step_start", title)
    try:
        yield
    except Exception as exc:
        emit("step_error", f"{title} failed: {exc}")
        raise
    else:
        emit("step_end", title, duration_ms=round((time.perf_counter() - started) * 1000))
    finally:
        _current_step.reset(token)
