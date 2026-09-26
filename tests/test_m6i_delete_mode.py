"""M6i conversation-cleanup toggle: config default/persistence, the session
endpoint round trip, and CLI --keep-conversation override resolution."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from mai2srt.cli import _resolve_keep
from mai2srt.config import (
    Config, auto_delete_conversation, set_auto_delete_conversation,
)
from mai2srt.server.app import create_app


def _cfg(tmp_path):
    return Config(data_dir=tmp_path)


def test_config_default_on_and_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    # absent key = ON (the historic behavior: delete after success)
    assert auto_delete_conversation(cfg) is True
    set_auto_delete_conversation(cfg, False)
    assert auto_delete_conversation(cfg) is False
    set_auto_delete_conversation(cfg, True)
    assert auto_delete_conversation(cfg) is True


def test_session_endpoint_roundtrip(tmp_path):
    c = TestClient(create_app(_cfg(tmp_path)))
    r = c.get("/api/session")
    assert r.status_code == 200
    assert r.json()["delete_conversation"] is True

    r = c.put("/api/session/delete_mode", json={"delete": False})
    assert r.status_code == 200
    assert r.json()["delete"] is False
    assert c.get("/api/session").json()["delete_conversation"] is False

    c.put("/api/session/delete_mode", json={"delete": True})
    assert c.get("/api/session").json()["delete_conversation"] is True


def test_cli_keep_resolution(tmp_path):
    cfg = _cfg(tmp_path)
    # flag absent -> config decides
    assert _resolve_keep(cfg, argparse.Namespace(keep_conversation=None)) is False
    set_auto_delete_conversation(cfg, False)   # user opted out -> keep
    assert _resolve_keep(cfg, argparse.Namespace(keep_conversation=None)) is True
    # explicit flag always wins, regardless of the config toggle
    assert _resolve_keep(cfg, argparse.Namespace(keep_conversation=True)) is True
    set_auto_delete_conversation(cfg, True)
    assert _resolve_keep(cfg, argparse.Namespace(keep_conversation=True)) is True
