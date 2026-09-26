"""Media tools must never open a console window.

A packaged build is a GUI process (PyInstaller console=False), so it has no
console to hand down and Windows gives every console child -- ffmpeg, ffprobe --
a black window of its own for the length of the call. Reported as "a black
window flashes when it re-extracts the audio from a video". A dev run starts
from a terminal and the child inherits that console, so nothing is visible
there: the bug exists only in the packaged app, which is exactly why it is worth
pinning from both ends -- the flags themselves, and a refusal to let a new call
site spawn a tool directly.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mai2srt.media import probe  # noqa: E402

MEDIA_SRC = Path(__file__).resolve().parents[1] / "src" / "mai2srt" / "media"


def test_no_window_asks_windows_to_hide_the_console():
    if sys.platform != "win32":
        pytest.skip("Windows-only concern")
    kwargs = probe.no_window()
    assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
    si = kwargs["startupinfo"]
    assert si.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert si.wShowWindow == subprocess.SW_HIDE


def test_no_window_is_a_no_op_elsewhere(monkeypatch):
    monkeypatch.setattr(probe.os, "name", "posix")
    assert probe.no_window() == {}


def test_run_tool_forwards_the_flags_and_the_callers_kwargs(monkeypatch):
    seen: dict = {}

    def fake(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(probe.subprocess, "run", fake)
    probe.run_tool(["ffprobe", "-v", "error"], capture_output=True, text=True, timeout=5)
    assert seen["cmd"] == ["ffprobe", "-v", "error"]
    assert seen["kwargs"]["capture_output"] is True
    assert seen["kwargs"]["text"] is True
    assert seen["kwargs"]["timeout"] == 5
    if sys.platform == "win32":
        assert seen["kwargs"]["creationflags"] == subprocess.CREATE_NO_WINDOW
        assert seen["kwargs"]["startupinfo"].wShowWindow == subprocess.SW_HIDE


def test_media_sources_only_spawn_through_run_tool():
    """Guards the NEXT call site: one that calls subprocess itself would flash a
    console window in the packaged app and pass every dev run."""
    spawns: dict[str, int] = {}
    for path in sorted(MEDIA_SRC.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        count = sum(text.count(p) for p in
                    ("subprocess.run(", "subprocess.Popen(", "subprocess.check_"))
        if count:
            spawns[path.name] = count
    # probe.py owns run_tool and is allowed exactly that one call
    assert spawns == {"probe.py": 1}, spawns


def test_a_real_call_still_works_with_the_flags(monkeypatch):
    """The flags must not break the call they protect. Uses the interpreter
    itself as the console child: always present, same spawn path."""
    r = probe.run_tool([sys.executable, "-c", "print('ok')"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"
