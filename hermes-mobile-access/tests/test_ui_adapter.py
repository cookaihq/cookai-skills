import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))
from adapter import Configuration
import adapter
from frp_client import FrpClient
from skill_ui.contracts import UIError


@pytest.fixture
def ui(tmp_path, monkeypatch):
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    monkeypatch.setattr(FrpClient, "context", lambda _: {
        "revision": "fixture-1", "domain_suffix": {"value": "frp.example.invalid", "status": "configured"}})
    monkeypatch.setattr(FrpClient, "status", lambda _: {"modes": {"official": {"running": False}}})
    monkeypatch.setattr(adapter, "read_profiles", lambda **_: {
        "result": "profiles_unavailable", "reason": "fixture_unavailable", "profiles": []})
    options = {"home": str(home), "use_local_key": True, "project": str(project),
               "frp_project": str(project), "frp_home": str(tmp_path / "frp"), "frp_skill": str(ROOT.parent / "frpc-launch")}
    return Configuration(project, options)


def test_real_env_save_and_reload_keeps_stable_id(ui):
    config = ui.config()
    saved = ui.save({"name": {"op": "set", "value": "家里的 Mac"},
                     "subdomain": {"op": "set", "value": "Mobile-Demo"}}, config["revision"])
    cid = saved["task"]["id"]
    assert saved["note"].startswith("使用真实配置")
    assert ui.store.snapshot().select()[1]["domain_prefix"] == "mobile-demo"
    again = Configuration(ui.project, ui.options)
    assert again.config()["task"]["id"] == cid
    assert (ui.store.home / ".env").is_file()


def test_active_domain_change_preserves_configuration_but_rename_is_safe(ui, monkeypatch):
    ui.save({"name": {"op": "set", "value": "连接一"}}, ui.config()["revision"])
    monkeypatch.setattr(FrpClient, "status", lambda _: {"modes": {"official": {"running": True}}})
    before = (ui.store.home / ".env").read_bytes()
    with pytest.raises(UIError) as caught:
        ui.save({"subdomain": {"op": "set", "value": "another"}}, ui.config()["revision"])
    assert caught.value.code == "preflight_unknown"
    assert (ui.store.home / ".env").read_bytes() == before
    original = ui.store.snapshot().select()[0]
    ui.save({"name": {"op": "set", "value": "重命名"}}, ui.config()["revision"])
    assert ui.store.snapshot().select()[0] == original


def test_readonly_injection_and_changed_frp_context_rejected(ui, monkeypatch):
    config = ui.config()
    with pytest.raises(UIError):
        ui.save({"suffix": {"op": "set", "value": "attacker.test"}}, config["revision"])
    monkeypatch.setattr(FrpClient, "context", lambda _: {
        "revision": "fixture-2", "domain_suffix": {"value": "new.test"}})
    with pytest.raises(UIError) as caught:
        ui.save({"subdomain": {"op": "set", "value": "another"}}, config["revision"])
    assert caught.value.code == "conflict"
    assert not (ui.store.home / ".env").exists()


def test_update_toggle_does_not_create_or_replace_connections(ui):
    ui.save({"update": {"op": "set", "value": False}}, ui.config()["revision"])
    assert not ui.store.snapshot().collection["connections"]
    assert ui.store.update_preference()[0] is False
    ui.save({"name": {"op": "set", "value": "Mac"}, "update": {"op": "set", "value": True}}, ui.config()["revision"])
    assert ui.store.snapshot().select()[1]["name"] == "Mac"
    assert ui.store.update_preference()[0] is True


@pytest.mark.parametrize("running,expected", [
    (True, "入口尚未验证"), (False, "未连接"), (None, "状态无法核对")])
def test_frp_process_and_route_do_not_establish_public_https_endpoint(ui, monkeypatch, running, expected):
    monkeypatch.setattr(FrpClient, "status", lambda _: {
        "modes": {"official": {"running": running}},
        "route": {"domain": "old.frp.example.invalid", "proxy": {
            "name": "hma-fixture", "localIP": "127.0.0.1", "localPort": 53219}}})
    fields = {item["id"]: item["value"] for item in ui.config()["fields"]}
    assert fields["active_url"] == expected
    assert fields["frp_domain"] == "old.frp.example.invalid"
    assert fields["proxy"] == "hma-fixture"
    assert fields["local_target"] == ("127.0.0.1:53219" if running else "未绑定")


def live_profiles(ui, monkeypatch):
    home = str((ui.project / "hermes").resolve())
    profile = str((ui.project / "hermes/profiles/work").resolve())
    result = {"result": "profiles_verified", "revision": "profiles-1", "home": home,
              "checked_at": "2026-11-07T00:00:00Z", "profiles": [
                  {"name": "work", "path": profile, "is_default": False, "served": True},
                  {"name": "offline", "path": str(ui.project / "offline"), "is_default": False, "served": False}]}
    monkeypatch.setattr(adapter, "read_profiles", lambda **_: result)
    return result


def test_profile_selection_saves_verified_home_and_profile_only(ui, monkeypatch):
    result = live_profiles(ui, monkeypatch)
    config = ui.config()
    field = next(row for row in config["fields"] if row["id"] == "profile")
    assert field["choices"] == ["work"] and not field["readonly"]
    ui.save({"profile": {"op": "set", "value": "work"}}, config["revision"])
    record = ui.store.snapshot().select()[1]
    assert record["hermes"] == {"home": result["home"], "preferred_profile": "work",
                                "profile_home": result["profiles"][0]["path"]}
    assert not list((ui.project / "hermes").glob("**/*"))


def test_unknown_or_unserved_profile_cannot_be_saved(ui, monkeypatch):
    live_profiles(ui, monkeypatch)
    for name in ("invented", "offline"):
        with pytest.raises(UIError) as caught:
            ui.save({"profile": {"op": "set", "value": name}}, ui.config()["revision"])
        assert caught.value.code == "profile_unavailable"
    assert not (ui.store.home / ".env").exists()


def test_profile_identity_refresh_prevents_stale_save(ui, monkeypatch):
    result = live_profiles(ui, monkeypatch)
    before = ui.config()
    monkeypatch.setattr(adapter, "read_profiles", lambda **_: dict(result, revision="profiles-2"))
    with pytest.raises(UIError) as caught:
        ui.save({"profile": {"op": "set", "value": "work"}}, before["revision"])
    assert caught.value.code == "conflict"
    assert not (ui.store.home / ".env").exists()


def test_foreign_instance_keeps_saved_binding_readonly(ui, monkeypatch):
    result = live_profiles(ui, monkeypatch)
    ui.save({"profile": {"op": "set", "value": "work"}}, ui.config()["revision"])
    original = (ui.store.home / ".env").read_bytes()
    foreign = dict(result, home=str(ui.project / "another"), revision="other-instance")
    monkeypatch.setattr(adapter, "read_profiles", lambda **_: foreign)
    ui._profile_cache = None
    page = ui.config()
    assert next(row for row in page["fields"] if row["id"] == "profile")["readonly"]
    with pytest.raises(UIError) as caught:
        ui.save({"profile": {"op": "set", "value": "work"}}, page["revision"])
    assert caught.value.code == "profile_unverified"
    assert (ui.store.home / ".env").read_bytes() == original


def test_key_save_masking_rotation_clear_and_rename(ui, monkeypatch):
    import json
    from access_security import generate_key, binding_digest
    from skill_ui.contracts import public_config
    key = generate_key()
    saved = ui.save({'access_key': {'op': 'set', 'value': key}}, ui.config()['revision'])
    snap = ui.store.snapshot()
    cid, record = snap.select()
    assert record['auth'] == {'type': 'mobile_key_v1', 'key': key}
    public = public_config(saved)
    assert key not in json.dumps(public)
    field = next(row for row in public['fields'] if row['id'] == 'access_key')
    assert field['configured'] and 'value' not in field
    with pytest.raises(UIError):
        ui.save({'access_key': {'op': 'set', 'value': field['mask']}}, saved['revision'])
    original = binding_digest(snap.source, cid, record)
    monkeypatch.setattr(FrpClient, 'status', lambda _: {'modes': {'official': {'running': True}}})
    saved = ui.save({'name': {'op': 'set', 'value': '重命名'}}, ui.config()['revision'])
    assert binding_digest(snap.source, cid, ui.store.snapshot().select()[1]) == original
    replacement = generate_key()
    saved = ui.save({'access_key': {'op': 'set', 'value': replacement}}, saved['revision'])
    assert ui.store.snapshot().select()[1]['auth']['key'] == replacement
    assert replacement not in json.dumps(public_config(saved))
    saved = ui.save({'access_key': {'op': 'unset'}}, saved['revision'])
    assert ui.store.snapshot().select()[1]['auth'] is None
    assert key not in json.dumps(saved) and replacement not in json.dumps(saved)


def test_invalid_key_or_stale_revision_cannot_partially_save(ui):
    from access_security import generate_key
    page = ui.config()
    with pytest.raises(UIError):
        ui.save({'name': {'op': 'set', 'value': 'should-not-save'},
                 'access_key': {'op': 'set', 'value': 'invalid'}}, page['revision'])
    assert not ui.store.snapshot().collection['connections']
    ui.save({'access_key': {'op': 'set', 'value': generate_key()}}, page['revision'])
    before = (ui.store.home / '.env').read_bytes()
    with pytest.raises(UIError) as caught:
        ui.save({'access_key': {'op': 'unset'}}, page['revision'])
    assert caught.value.code == 'conflict'
    assert (ui.store.home / '.env').read_bytes() == before


def test_http_ui_save_reveal_and_receipt_never_publish_key(ui, tmp_path):
    import http.client
    import json
    import threading
    from access_security import generate_key
    from skill_ui.server import Server
    directory = tmp_path / 'ui-runtime'
    directory.mkdir()
    server = Server(ui, ui.project, 'configuration', directory)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    common = {'Origin': server.origin, 'Content-Type': 'application/json'}
    def request(method, path, data=None):
        client.request(method, path, body=json.dumps(data) if data is not None else None, headers=common)
        response = client.getresponse()
        return response.status, response.getheader('Set-Cookie'), response.read()
    key = generate_key()
    try:
        status, cookie, raw = request('POST', '/api/session', {'token': server.token})
        assert status == 200
        common.update({'Cookie': cookie.split(';', 1)[0], 'X-CSRF-Token': json.loads(raw)['csrf']})
        status, _, raw = request('GET', '/api/state')
        revision = json.loads(raw)['config']['revision']
        status, _, raw = request('POST', '/api/save', {'revision': revision,
            'changes': {'access_key': {'op': 'set', 'value': key}}})
        assert status == 200 and key.encode() not in raw
        saved = json.loads(raw)
        assert key.encode() not in (directory / 'saved.json').read_bytes()
        status, _, raw = request('POST', '/api/reveal', {'id': 'access_key', 'revision': revision})
        assert status == 409 and key.encode() not in raw
        status, _, raw = request('POST', '/api/reveal', {'id': 'access_key', 'revision': saved['config']['revision']})
        assert status == 200 and json.loads(raw)['value'] == key
        status, _, raw = request('GET', '/api/state')
        assert status == 200 and key.encode() not in raw
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(2)
