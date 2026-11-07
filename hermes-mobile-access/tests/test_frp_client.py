import subprocess
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from configuration import ConfigError
from frp_client import FrpClient


def test_process_permission_error_keeps_failure_layer_without_secret(tmp_path, monkeypatch):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/frpc_launch.py").touch()
    def denied(*args, **kwargs):
        raise PermissionError(13, "secret-must-not-be-forwarded")
    monkeypatch.setattr(subprocess, "run", denied)
    with pytest.raises(ConfigError) as caught:
        FrpClient(tmp_path, tmp_path, tmp_path).context()
    assert caught.value.code == "frp_process_start_failed"
    assert "PermissionError" in str(caught.value) and "errno=13" in str(caught.value)
    assert "secret-must-not" not in str(caught.value)


def test_foreign_pid_cleanup_is_not_reported_as_stopped(tmp_path, monkeypatch):
    client = FrpClient(tmp_path, tmp_path, tmp_path, "hma_" + "a" * 32)
    monkeypatch.setattr(client, "call", lambda *args, **kwargs: {
        "modes": {"official": {"result": "stale_pid_cleaned"}}})
    with pytest.raises(ConfigError) as caught:
        client.stop()
    assert caught.value.code == "cleanup_unverified"


def test_owned_exited_runner_can_be_cleaned_then_reconnected(tmp_path, monkeypatch):
    client = FrpClient(tmp_path, tmp_path, tmp_path, 'hma_' + 'a' * 32)
    monkeypatch.setattr(client, 'call', lambda *a, **k: {
        'modes': {'official': {'result': 'stale_pid_cleaned', 'owned_process_exited': True}}})
    monkeypatch.setattr(client, 'status', lambda: {'modes': {'official': {'running': False}}})
    assert client.stop()['modes']['official']['running'] is False


@pytest.mark.parametrize("status,expected", [
    ({}, "cleanup_unverified"),
    ({"modes": {"official": {"running": None}}}, "cleanup_unverified"),
    ({"modes": {"official": {"running": True}}}, "cleanup_failed"),
    ({"modes": {"official": {"running": False}}}, None),
])
def test_stop_acknowledgement_requires_verified_process_status(tmp_path, monkeypatch, status, expected):
    client = FrpClient(tmp_path, tmp_path, tmp_path, "hma_" + "a" * 32)
    monkeypatch.setattr(client, "call", lambda *args, **kwargs: {
        "modes": {"official": {"result": "stopped"}}})
    monkeypatch.setattr(client, "status", lambda: status)
    if expected:
        with pytest.raises(ConfigError) as caught:
            client.stop()
        assert caught.value.code == expected
    else:
        assert client.stop() == status
