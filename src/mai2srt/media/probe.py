"""ffprobe/ffmpeg resolution and media probing."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class MediaError(RuntimeError):
    pass


#: where a resolved tool came from: the copy that belongs to the app (shipped
#: beside a frozen sidecar, or vendored in a checkout), or the machine's own
#: install found on PATH
SOURCE_BUNDLED = "bundled"
SOURCE_PATH = "path"

#: markers that identify the root of a source checkout
_REPO_MARKERS = ("pyproject.toml", ".git")


def _repo_root() -> Path | None:
    """The checkout this module lives in, or None when it does not live in one."""
    for parent in Path(__file__).resolve().parents:
        if any((parent / m).exists() for m in _REPO_MARKERS):
            return parent
    return None


def resolve_tool_ex(name: str) -> tuple[str, str]:
    """(executable, source) for ffmpeg/ffprobe.

    Order: the copy that belongs to the app first, then PATH.

    - packaged: the copy staged beside the frozen sidecar
    - checkout: the copy this project vendors for packaging (vendor/ffmpeg,
      fetched by packaging/fetch_ffmpeg.ps1)

    A dev run preferring the vendored copy is the point, not a convenience: it
    is byte-identical to what ships, while a developer's own ffmpeg is a
    DIFFERENT build with different libraries -- a filter or codec present in one
    and missing from the other passes in dev and fails for every user. PATH
    stays the fallback for a checkout that never fetched the binaries.

    Reporting the source is what lets the settings card say which of the two
    won; a bare path looks the same either way.
    """
    if getattr(sys, "frozen", False):
        bundled = Path(sys.executable).parent / "ffmpeg" / f"{name}.exe"
        if bundled.is_file():
            return str(bundled), SOURCE_BUNDLED
    else:
        root = _repo_root()
        if root is not None:
            vendored = root / "vendor" / "ffmpeg" / f"{name}.exe"
            if vendored.is_file():
                return str(vendored), SOURCE_BUNDLED
    path = shutil.which(name)
    if not path:
        raise MediaError(
            f"{name} not found; install FFmpeg "
            "(https://ffmpeg.org) or add it to PATH")
    return path, SOURCE_PATH


def resolve_tool(name: str) -> str:
    """The executable alone, for call sites that only want to run it."""
    return resolve_tool_ex(name)[0]


def no_window() -> dict:
    """subprocess kwargs that keep a console tool from opening a window.

    A packaged build is a GUI process (PyInstaller console=False), so it has no
    console to pass down -- and Windows then gives every console child of its
    own, meaning a black window for as long as ffmpeg or ffprobe runs. A dev run
    starts from a terminal and the child inherits that console, so the window is
    only ever seen in the packaged app, which is what makes it worth pinning.
    """
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": si}


def run_tool(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run for ffmpeg/ffprobe, with the console window suppressed.

    Every media call has to go through here: one that does not will flash a
    console window in the packaged app and nowhere else, so it survives every
    dev test.
    """
    return subprocess.run(cmd, **no_window(), **kwargs)


@dataclass
class MediaInfo:
    duration_s: float
    channels: int
    sample_rate: int
    codec: str
    ext: str          # lower-case extension without dot
    size_bytes: int

    @property
    def mime(self) -> str | None:
        return {"mp3": "audio/mpeg", "wav": "audio/wav"}.get(self.ext)


def probe(path: str | Path) -> MediaInfo:
    """ffprobe format+stream info for an audio file."""
    ffprobe = resolve_tool("ffprobe")
    p = Path(path)
    r = run_tool(
        [ffprobe, "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(p)],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120,
    )
    if r.returncode != 0:
        raise MediaError(f"ffprobe failed for {p}: {r.stderr[-300:]}")
    try:
        data = json.loads(r.stdout)
        fmt = data["format"]
        audio = next((s for s in data.get("streams", [])
                      if s.get("codec_type") == "audio"), None)
        if audio is None:
            # trackless videos (and non-media files that ffprobe sniffs)
            # used to escape as a bare StopIteration -> 500
            raise MediaError(f"no audio stream in {p}")
        return MediaInfo(
            duration_s=float(fmt["duration"]),
            channels=int(audio.get("channels") or 0),
            sample_rate=int(audio.get("sample_rate") or 0),
            codec=str(audio.get("codec_name") or "?"),
            ext=p.suffix.lstrip(".").lower(),
            size_bytes=p.stat().st_size,
        )
    except (KeyError, ValueError, StopIteration) as e:
        raise MediaError(f"cannot parse ffprobe output for {p}: {e}") from e
