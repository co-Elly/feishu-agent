from types import SimpleNamespace

import pytest

import gateway_guard


def test_same_app_active_hermes_gateway_is_rejected(monkeypatch):
    monkeypatch.setattr(gateway_guard, "_active_hermes_gateway_app_id", lambda: "cli_same")
    with pytest.raises(gateway_guard.CompetingGatewayError, match="同一飞书 App ID"):
        gateway_guard.assert_no_competing_hermes_gateway("cli_same")


def test_different_or_inactive_hermes_gateway_is_allowed(monkeypatch):
    monkeypatch.setattr(gateway_guard, "_active_hermes_gateway_app_id", lambda: "cli_other")
    gateway_guard.assert_no_competing_hermes_gateway("cli_this")
    monkeypatch.setattr(gateway_guard, "_active_hermes_gateway_app_id", lambda: None)
    gateway_guard.assert_no_competing_hermes_gateway("cli_this")


def test_gateway_detection_reads_id_when_service_or_process_is_active(monkeypatch):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if "pgrep" in command[-1]:
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout="cli_app")

    monkeypatch.setattr(gateway_guard.os, "name", "nt")
    monkeypatch.setattr(gateway_guard.shutil, "which", lambda _name: "wsl.exe")
    monkeypatch.setattr(gateway_guard.subprocess, "run", run)
    assert gateway_guard._active_hermes_gateway_app_id() == "cli_app"
    assert len(calls) == 2


def test_gateway_detection_skips_id_when_no_service_or_process(monkeypatch):
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(gateway_guard.os, "name", "nt")
    monkeypatch.setattr(gateway_guard.shutil, "which", lambda _name: "wsl.exe")
    monkeypatch.setattr(gateway_guard.subprocess, "run", run)
    assert gateway_guard._active_hermes_gateway_app_id() is None
    assert len(calls) == 1
