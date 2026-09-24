"""Tests für app/supervisor_stats.py — insbesondere restart_addon() (Konzept
"Restore-Rollbacks", 22.09.2026: der "Jetzt neu starten"-Dialog nach einem
vorgemerkten Restore). fetch_memory_usage_bytes() folgt demselben Muster,
bislang aber ungetestet — hier nur das für restart_addon() Nötige."""

from __future__ import annotations

import contextlib
import io
import json
import urllib.error

import pytest

from app import supervisor_stats


def test_restart_addon_requires_a_supervisor_token(monkeypatch) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="Supervisor ist in dieser Umgebung nicht verfügbar"):
        supervisor_stats.restart_addon()


def test_restart_addon_succeeds_on_a_plain_ok_result(monkeypatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["auth"] = request.get_header("Authorization")
        return contextlib.closing(io.BytesIO(json.dumps({"result": "ok"}).encode()))

    monkeypatch.setattr(supervisor_stats.urllib.request, "urlopen", fake_urlopen)
    supervisor_stats.restart_addon()  # wirft nicht
    assert seen["url"] == supervisor_stats.SUPERVISOR_RESTART_URL
    assert seen["method"] == "POST"
    assert seen["auth"] == "Bearer test-token"


def test_restart_addon_raises_on_a_non_ok_result(monkeypatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

    def fake_urlopen(request, timeout=None):
        body = json.dumps({"result": "error", "message": "addon busy"}).encode()
        return contextlib.closing(io.BytesIO(body))

    monkeypatch.setattr(supervisor_stats.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="addon busy"):
        supervisor_stats.restart_addon()


def test_restart_addon_raises_on_a_connection_error(monkeypatch) -> None:
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")

    def raise_connection_error(request, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(supervisor_stats.urllib.request, "urlopen", raise_connection_error)
    with pytest.raises(RuntimeError, match="Neustart über Supervisor fehlgeschlagen"):
        supervisor_stats.restart_addon()
