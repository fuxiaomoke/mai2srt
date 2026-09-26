"""Which ffmpeg runs, and can the settings card say which one it was?

The copy that belongs to the app must win over the machine's own: in a packaged
build that is the one staged beside the sidecar, in a checkout the one vendored
for packaging (vendor/ffmpeg, fetched by packaging/fetch_ffmpeg.ps1). Both are
byte-identical to what ships, while a developer's ffmpeg is a DIFFERENT build --
different libraries -- so testing against it proves nothing about the packaged
app, and it is exactly what "the settings card shows the ffmpeg I already had on
D:" looked like. PATH is only the fallback for a checkout that never fetched it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

import mai2srt.media.probe as probe_mod  # noqa: E402
from mai2srt.config import Config  # noqa: E402
from mai2srt.media.probe import (  # noqa: E402
    SOURCE_BUNDLED,
    SOURCE_PATH,
    MediaError,
    resolve_tool,
    resolve_tool_ex,
)
from mai2srt.server.app import create_app  # noqa: E402

PATH_FFMPEG = r"D:\ffmpeg-9.0-full_build\bin\ffmpeg.EXE"


def _write(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _sidecar(tmp_path: Path, *, bundle: bool) -> Path:
    """A fake frozen layout: the sidecar exe, optionally with ffmpeg beside it."""
    exe = _write(tmp_path / "mai2srt-backend.exe")
    if bundle:
        _write(tmp_path / "ffmpeg" / "ffmpeg.exe")
    return exe


def _checkout(tmp_path: Path, *, vendored: bool) -> Path:
    """A fake source checkout: pyproject marker, optionally with vendor/ffmpeg."""
    root = tmp_path / "checkout"
    _write(root / "pyproject.toml")
    if vendored:
        _write(root / "vendor" / "ffmpeg" / "ffmpeg.exe")
    return root


# ------------------------------------------------------------------ packaged

def test_frozen_prefers_the_copy_beside_the_sidecar(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(_sidecar(tmp_path, bundle=True)))
    monkeypatch.setattr("shutil.which", lambda name: PATH_FFMPEG)
    path, source = resolve_tool_ex("ffmpeg")
    assert source == SOURCE_BUNDLED
    assert Path(path) == tmp_path / "ffmpeg" / "ffmpeg.exe"


def test_frozen_without_a_bundle_falls_back_to_path(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(_sidecar(tmp_path, bundle=False)))
    monkeypatch.setattr("shutil.which", lambda name: PATH_FFMPEG)
    assert resolve_tool_ex("ffmpeg") == (PATH_FFMPEG, SOURCE_PATH)


def test_a_frozen_install_never_hunts_the_filesystem(monkeypatch, tmp_path):
    """No sidecar copy: a checkout found on disk must not be borrowed."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(_sidecar(tmp_path, bundle=False)))
    monkeypatch.setattr(probe_mod, "_repo_root", lambda: _checkout(tmp_path, vendored=True))
    monkeypatch.setattr("shutil.which", lambda name: PATH_FFMPEG)
    assert resolve_tool_ex("ffmpeg") == (PATH_FFMPEG, SOURCE_PATH)


# ------------------------------------------------------------------ checkout

def test_checkout_prefers_the_vendored_copy(monkeypatch, tmp_path):
    """The whole point: dev must run the build that ships, not the PATH one."""
    root = _checkout(tmp_path, vendored=True)
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(probe_mod, "_repo_root", lambda: root)
    monkeypatch.setattr("shutil.which", lambda name: PATH_FFMPEG)
    path, source = resolve_tool_ex("ffmpeg")
    assert source == SOURCE_BUNDLED
    assert Path(path) == root / "vendor" / "ffmpeg" / "ffmpeg.exe"


def test_checkout_without_vendored_binaries_uses_path(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(probe_mod, "_repo_root", lambda: _checkout(tmp_path, vendored=False))
    monkeypatch.setattr("shutil.which", lambda name: PATH_FFMPEG)
    assert resolve_tool_ex("ffmpeg") == (PATH_FFMPEG, SOURCE_PATH)


def test_no_checkout_at_all_uses_path(monkeypatch, tmp_path):
    """An installed (non-editable) package sits in site-packages: no markers."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(probe_mod, "_repo_root", lambda: None)
    monkeypatch.setattr("shutil.which", lambda name: PATH_FFMPEG)
    assert resolve_tool_ex("ffmpeg") == (PATH_FFMPEG, SOURCE_PATH)


def test_a_copy_beside_python_is_not_a_bundle(monkeypatch, tmp_path):
    """The frozen layout lives beside the sidecar, not beside the interpreter."""
    exe = _write(tmp_path / "python.exe")
    _write(tmp_path / "ffmpeg" / "ffmpeg.exe")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(probe_mod, "_repo_root", lambda: None)
    monkeypatch.setattr("shutil.which", lambda name: PATH_FFMPEG)
    assert resolve_tool_ex("ffmpeg") == (PATH_FFMPEG, SOURCE_PATH)


# --------------------------------------------------------------------- edges

def test_resolve_tool_still_returns_the_bare_path(monkeypatch, tmp_path):
    """Call sites that only run the tool must keep their signature."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(_sidecar(tmp_path, bundle=True)))
    assert resolve_tool("ffmpeg") == str(tmp_path / "ffmpeg" / "ffmpeg.exe")


def test_missing_tool_raises_with_the_name(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(probe_mod, "_repo_root", lambda: None)
    monkeypatch.setattr("shutil.which", lambda name: None)
    try:
        resolve_tool_ex("ffmpeg")
    except MediaError as e:
        assert "ffmpeg" in str(e)
    else:
        raise AssertionError("a missing tool must not resolve silently")


def test_system_endpoint_reports_the_source(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    client = TestClient(create_app(Config(data_dir=tmp_path)))
    body = client.get("/api/system").json()
    assert set(body["ffmpeg"]) >= {"found", "path", "source"}
    assert body["ffmpeg"]["source"] in (None, SOURCE_BUNDLED, SOURCE_PATH)
