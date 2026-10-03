"""Background job manager: lifecycle, SSE event bus, cancellation, mutex.

One transcription-class job at a time (mutex); ``process``/``login`` jobs run
through the same machinery. Events are buffered per job so a client that
connects late (or reconnects) still sees the full history, then streams live.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config

log = logging.getLogger("mai2srt.jobs")


@dataclass
class Job:
    id: str
    kind: str                      # run | process | login | consent
    title: str
    created_at: float = field(default_factory=time.time)
    status: str = "queued"         # queued | running | done | error | cancelled
    events: list[dict] = field(default_factory=list)   # full history
    _waiters: list[asyncio.Event] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    task: asyncio.Task | None = None


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._transcribe_lock = asyncio.Lock()
        # captured at first start(); emit/cancel may arrive from worker
        # threads (asyncio.to_thread offload) and must marshal back
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None

    # -- queries ---------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at,
                      reverse=True)

    def busy(self) -> bool:
        return any(j.status in ("queued", "running")
                   for j in self._jobs.values())

    # -- creation --------------------------------------------------------

    async def start(
        self,
        kind: str,
        title: str,
        coro_factory: Callable[[Job], Any],
    ) -> Job:
        """Create and run a job. ``coro_factory(job)`` returns an awaitable.

        The factory receives the Job so it can emit events and check for
        cooperative cancellation via the asyncio task.
        """
        if self.busy():
            raise BusyError("a job is already running")
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
            self._loop_thread = threading.current_thread()
        job = Job(id="%s-%d" % (kind, next(self._ids)), kind=kind, title=title)
        self._jobs[job.id] = job
        job.status = "running"
        job.task = asyncio.create_task(self._run(job, coro_factory))
        return job

    async def _run(self, job: Job, coro_factory: Callable[[Job], Any]) -> None:
        try:
            result = await coro_factory(job)
            job.result = result
            job.status = "done"
            self.emit(job, "done", result or {})
        except asyncio.CancelledError:
            # clear the pending-cancel flag so the task settles normally;
            # status was already set authoritatively by cancel() -- only
            # emit here when the cancellation came from somewhere else
            if job.task is not None:
                job.task.uncancel()
            if job.status != "cancelled":
                job.status = "cancelled"
                self.emit(job, "cancelled", {})
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = str(e)
            # persist the FULL error (UI toasts truncate at ~160 chars)
            log.error("job %s (%s) failed: %s", job.id, job.kind, e)
            self.emit(job, "error", {"message": str(e), "type": type(e).__name__})
        finally:
            self._wake(job)

    # -- events ------------------------------------------------------------

    def emit(self, job: Job, type_: str, data: dict) -> None:
        """Append + wake. Safe from worker threads: asyncio.Event.set() is
        not thread-safe, so off-loop emissions are marshalled onto the loop."""
        ev = {"type": type_, "data": data, "ts": time.time()}
        if (self._loop is not None and self._loop_thread is not None
                and threading.current_thread() is not self._loop_thread):
            self._loop.call_soon_threadsafe(self._emit_now, job, ev)
        else:
            self._emit_now(job, ev)

    def _emit_now(self, job: Job, ev: dict) -> None:
        job.events.append(ev)
        self._wake(job)

    def _wake(self, job: Job) -> None:
        for w in job._waiters:
            w.set()
        job._waiters.clear()

    async def stream(self, job: Job, from_idx: int = 0):
        """Yield events from ``from_idx`` onward; returns when job settles."""
        idx = from_idx
        while True:
            while idx < len(job.events):
                ev = job.events[idx]
                idx += 1
                yield ev
            if job.status in ("done", "error", "cancelled"):
                return
            waiter = asyncio.Event()
            job._waiters.append(waiter)
            # small race guard: re-check after registering
            if idx < len(job.events) or job.status in ("done", "error", "cancelled"):
                job._waiters.remove(waiter)
                continue
            await waiter.wait()

    # -- cancellation ------------------------------------------------------

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        # authoritative immediately: a task cancelled before its first
        # suspension point never enters _run, so the except branch below
        # would never fire (task just settles as cancelled).
        job.status = "cancelled"
        if job.task:
            # cancel() is reached from a sync FastAPI endpoint (threadpool);
            # Task.cancel() must run on the owning loop
            if (self._loop is not None and self._loop_thread is not None
                    and threading.current_thread() is not self._loop_thread):
                self._loop.call_soon_threadsafe(job.task.cancel)
            else:
                job.task.cancel()
        self.emit(job, "cancelled", {})
        return True


class BusyError(RuntimeError):
    pass
