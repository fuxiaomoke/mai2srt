"""CORS regression guard.

The dev page origin must never be pinned to one hardcoded port again: when
the Vite server moved 5173 -> 5183 the stale allowlist made the browser
block every API response while uvicorn kept logging 200 OK, so the UI said
"backend offline" against a perfectly healthy backend.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from mai2srt.config import Config
from mai2srt.server.app import create_app


def _client(tmp_path):
    return TestClient(create_app(Config(data_dir=tmp_path)))


def test_local_dev_origins_allowed(tmp_path):
    c = _client(tmp_path)
    for origin in ("http://localhost:5183",      # current dev port
                   "http://127.0.0.1:5183",
                   "http://localhost:5173",      # legacy port: still fine
                   "http://localhost:4000"):     # any other local port
        r = c.get("/api/params", headers={"Origin": origin})
        assert r.status_code == 200, origin
        assert r.headers.get("access-control-allow-origin") == origin, origin


def test_tauri_origin_allowed(tmp_path):
    c = _client(tmp_path)
    # macOS/Linux packaged webview
    r = c.get("/api/params", headers={"Origin": "tauri://localhost"})
    assert r.headers.get("access-control-allow-origin") == "tauri://localhost"
    # Windows packaged webview: WebView2 cannot use custom schemes, so
    # Tauri serves the frontend from http://tauri.localhost. Missing this
    # form made every INSTALLED-build fetch CORS-blocked client-side while
    # uvicorn logged 200 OK (the backend-offline bug that only reproduced
    # in packaged apps).
    r = c.get("/api/params", headers={"Origin": "http://tauri.localhost"})
    assert r.headers.get("access-control-allow-origin") == "http://tauri.localhost"
    # preflight (PUT/POST with content-type) must clear too
    r = c.options("/api/params", headers={
        "Origin": "http://tauri.localhost",
        "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "content-type",
    })
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://tauri.localhost"


def test_remote_origins_rejected(tmp_path):
    c = _client(tmp_path)
    for origin in ("http://evil.example.com", "https://localhost.evil.com",
                   "http://notlocalhost:5183"):
        r = c.get("/api/params", headers={"Origin": origin})
        assert r.status_code == 200
        assert "access-control-allow-origin" not in r.headers, origin


def test_preflight_allows_local_dev_origin(tmp_path):
    c = _client(tmp_path)
    r = c.options("/api/params", headers={
        "Origin": "http://localhost:5183",
        "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "content-type",
    })
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5183"
