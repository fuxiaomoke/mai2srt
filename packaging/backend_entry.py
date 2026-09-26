"""PyInstaller entry for the frozen mai2srt backend.

Two frozen-only concerns live here, NOT in the app package:

1. Windowed builds (console=False, so no black console window flashes when
   the Tauri shell spawns the sidecar) leave sys.stdout/sys.stderr as None,
   and uvicorn's logging would crash on first emit. Redirect both to a log
   file under the data dir -- that file is also the ONLY window into a
   packaged backend failure, so it must always exist.
2. Everything else just forwards to the normal CLI (serve --port ...).
"""
from __future__ import annotations

import sys
from pathlib import Path


def _redirect_streams() -> None:
    if not getattr(sys, "frozen", False):
        return
    # UNCONDITIONAL in frozen builds, not just when the streams are None:
    # when the Tauri shell spawns us, stdout/stderr ARE set (pipes for the
    # shell's event stream) -- and if nobody drains them, uvicorn's access
    # log fills the pipe within minutes and the backend blocks mid-write.
    # The log file is also the ONLY window into a packaged backend failure.
    logdir = Path.home() / ".mai2srt" / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    fh = open(logdir / "backend.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = fh
    sys.stderr = fh


def main() -> int:
    _redirect_streams()
    from mai2srt.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
