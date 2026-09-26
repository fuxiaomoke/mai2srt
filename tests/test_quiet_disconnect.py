"""The Windows proactor disconnect-noise filter: swallows exactly the
ConnectionResetError-from-_call_connection_lost combination (cancelled
audio Range requests) and forwards everything else to the default
handler."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.server.app import quiet_disconnect_handler


class SpyLoop(asyncio.AbstractEventLoop):
    """records default_handler invocations; never actually runs"""

    def __init__(self) -> None:  # noqa: D107
        self.default_calls: list[dict] = []
        self._handler = None

    def default_exception_handler(self, context):
        self.default_calls.append(context)

    def set_exception_handler(self, handler):
        self._handler = handler

    def call_soon(self, *a, **k): ...
    def call_later(self, *a, **k): ...
    def call_at(self, *a, **k): ...
    def run_forever(self): ...
    def stop(self): ...
    def is_running(self):
        return False

    def is_closed(self):
        return False

    def close(self): ...

    def time(self):
        return 0.0


def test_filter_swallows_cancelled_range_reset():
    loop = SpyLoop()
    quiet_disconnect_handler(loop)
    assert loop._handler is not None
    # the exact shape asyncio emits for a cancelled audio range request
    loop._handler(loop, {
        "message": ("Exception in callback "
                    "_ProactorBasePipeTransport._call_connection_lost()"),
        "exception": ConnectionResetError(10054, "remote forced close"),
        "handle": None,
    })
    assert loop.default_calls == []


def test_filter_forwards_everything_else():
    loop = SpyLoop()
    quiet_disconnect_handler(loop)
    # reset OUTSIDE the connection-lost callback -> still interesting
    loop._handler(loop, {
        "message": "Exception in callback Task.wakeup()",
        "exception": ConnectionResetError(10054, "remote forced close"),
    })
    # other exceptions in the connection-lost callback -> still loud
    loop._handler(loop, {
        "message": ("Exception in callback "
                    "_ProactorBasePipeTransport._call_connection_lost()"),
        "exception": OSError("boom"),
    })
    # no exception object at all
    loop._handler(loop, {"message": "something odd"})
    assert len(loop.default_calls) == 3
