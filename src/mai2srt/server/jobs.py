"""Background job manager: lifecycle, SSE event bus, cancellation, mutex.

One transcription-class job at a time (mutex); ``process``/``login`` jobs run
through the same machinery. Events are buffered per job so a client that
connects late (or reconnects) still sees the full history, then streams live.

That replay is what lets the UI be a VIEW of a run instead of its owner: the
transcribe page may be unmounted, the webview may be reloaded, and the next
client attaches to the same buffered stream and paints the run it never saw.
Indices therefore have to mean something stable -- see ``JOB_EVENT_CAP`` and
``Job.dropped``: events are trimmed under memory pressure, but the INDEX of
every event that survives still matches the position it always had, so a
resuming client's ``Last-Event-ID`` can never be silently pointed at a
different record.

Two log streams come out of one job:
- ``log`` events without ``detail`` -- the short, plain lines a normal user
  reads (what stage it is on, how many pieces, what to do next);
- ``log`` events with ``detail: true`` -- the diagnostic stream, mirrored from
  the ``mai2srt`` logging tree by :class:`_JobDetailHandler`. The UI hides it
  behind a button, but it runs always: it is what makes a bug report useful
  without asking the reporter for numbers they never saw.
"""
from __future__ import annotations

import asyncio
import contextvars
import itertools
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config
from ..messages import user_message

log = logging.getLogger("mai2srt.jobs")

#: the job whose code is running right now; read by _JobDetailHandler so a
#: log record lands in the right job's stream (and in no job's stream at all
#: when nothing is running -- the file log still has it).
_current_job: contextvars.ContextVar["Job | None"] = contextvars.ContextVar(
    "mai2srt_current_job", default=None)


class _JobDetailHandler(logging.Handler):
    """Mirror every ``mai2srt.*`` log record into the running job's detail stream.

    Going through the logging tree instead of a hand-passed callback means
    anything that logs is captured automatically -- including code written
    later, which is the difference between a diagnostic stream that stays
    useful and one that rots.
    """

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s | %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        job = _current_job.get()
        if job is None or job.manager is None:
            return
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001 -- logging must never break a job
            return
        try:
            job.manager.emit(job, "log", {"line": line, "detail": True})
        except Exception:  # noqa: BLE001 -- by a dead loop, say
            pass


_detail_handler: _JobDetailHandler | None = None


#: How many events ONE job keeps in memory. The detail stream emits a record
#: per media step (ffmpeg line, per-chunk number), so a long run produces
#: thousands -- and a client that attaches late replays every one of them,
#: which is fine at this size (~4 MB worst case) and would not be at ten times
#: it. Trimmed from the FRONT (see ``Job.dropped``) so live streaming and index
#: arithmetic are unaffected; only a replay of the very beginning is lost, and
#: that is exactly the part nobody needs after the fact.
JOB_EVENT_CAP = 20000

#: Settled jobs kept around for replay. The UI attaches to the RUNNING job; the
#: settled ones are kept so a reload can still paint "just finished". Bounded so
#: a long session of many short jobs cannot grow the manager without limit.
MAX_SETTLED_JOBS = 8


def _attach_detail_handler() -> None:
    """Attach the mirror handler once per process (not once per app)."""
    global _detail_handler
    if _detail_handler is not None:
        return
    _detail_handler = _JobDetailHandler()
    logging.getLogger("mai2srt").addHandler(_detail_handler)


@dataclass
class Job:
    id: str
    kind: str                      # run | process | login | consent
    title: str
    created_at: float = field(default_factory=time.time)
    status: str = "queued"         # queued | running | done | error | cancelled
    events: list[dict] = field(default_factory=list)   # history (tail, see cap)
    #: events trimmed off the FRONT of ``events``. The public index of an event
    #: is ``dropped + position``: absolute, monotonic, and stable across a trim,
    #: so an SSE ``id:`` handed to the browser still names the same record when
    #: the browser resumes with it.
    dropped: int = 0
    _waiters: list[asyncio.Event] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    task: asyncio.Task | None = None
    manager: "JobManager | None" = None   # set at creation, used by the handler
    #: language of the copy this job shows the user (log lines AND the final
    #: error message), pinned when the job was created
    lang: str = "zh"


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._ids = itertools.count(1)
        self._transcribe_lock = asyncio.Lock()
        # captured at first start(); emit/cancel may arrive from worker
        # threads (asyncio.to_thread offload) and must marshal back
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        _attach_detail_handler()

    # -- queries ---------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at,
                      reverse=True)

    def busy(self) -> bool:
        return any(j.status in ("queued", "running")
                   for j in self._jobs.values())

    def _prune_settled(self) -> None:
        """Forget the oldest settled jobs beyond ``MAX_SETTLED_JOBS``.

        Only SETTLED ones: a running job must stay addressable, and a client
        that is still streaming a settled one holds the object itself, so
        dropping it from the map cannot cut a live stream -- it only means a
        brand-new client asking for that id gets 404 instead of a replay.
        """
        settled = [j for j in self._jobs.values()
                   if j.status in ("done", "error", "cancelled")]
        settled.sort(key=lambda j: j.created_at)
        for job in settled[:max(0, len(settled) - MAX_SETTLED_JOBS)]:
            self._jobs.pop(job.id, None)

    # -- creation --------------------------------------------------------

    async def start(
        self,
        kind: str,
        title: str,
        coro_factory: Callable[[Job], Any],
        lang: str = "zh",
    ) -> Job:
        """Create and run a job. ``coro_factory(job)`` returns an awaitable.

        The factory receives the Job so it can emit events and check for
        cooperative cancellation via the asyncio task. ``lang`` is the
        language of everything this job says to the user.
        """
        if self.busy():
            raise BusyError("a job is already running")
        self._prune_settled()
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
            self._loop_thread = threading.current_thread()
        job = Job(id="%s-%d" % (kind, next(self._ids)), kind=kind, title=title,
                  lang=lang)
        job.manager = self
        self._jobs[job.id] = job
        job.status = "running"
        job.task = asyncio.create_task(self._run(job, coro_factory))
        return job

    async def _run(self, job: Job, coro_factory: Callable[[Job], Any]) -> None:
        # bind this task (and every to_thread worker started from it) to the
        # job, so _JobDetailHandler knows where to send log records
        token = _current_job.set(job)
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
            # what the user reads: a short sentence in their language when the
            # exception carries a catalog code, else the raw message
            job.error = user_message(e, job.lang)
            # what the log/detail stream keeps: the full technical message
            log.error("job %s (%s) failed: %s", job.id, job.kind, e)
            self.emit(job, "error", {"message": job.error, "type": type(e).__name__})
        finally:
            _current_job.reset(token)
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
        overflow = len(job.events) - JOB_EVENT_CAP
        if overflow > 0:
            del job.events[:overflow]
            job.dropped += overflow
        self._wake(job)

    def _wake(self, job: Job) -> None:
        for w in job._waiters:
            w.set()
        job._waiters.clear()

    async def stream(self, job: Job, from_idx: int = 0):
        """Yield ``(index, event)`` pairs from ``from_idx`` onward.

        Indices are ABSOLUTE (see ``Job.dropped``), which is what the SSE
        ``id:`` field carries. ``from_idx`` may name an event this process has
        already trimmed: the stream then starts at the oldest record it still
        holds instead of failing -- a resuming client wants the tail, not an
        apology. Returns when the job settles.
        """
        idx = max(from_idx, job.dropped)
        while True:
            # a trim can land between two iterations (the emit that trims runs
            # on this same loop, so it never cuts in mid-batch)
            if idx < job.dropped:
                idx = job.dropped
            while idx - job.dropped < len(job.events):
                yield idx, job.events[idx - job.dropped]
                idx += 1
            if job.status in ("done", "error", "cancelled"):
                return
            waiter = asyncio.Event()
            job._waiters.append(waiter)
            # small race guard: re-check after registering
            if idx < job.dropped or idx - job.dropped < len(job.events) \
                    or job.status in ("done", "error", "cancelled"):
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
