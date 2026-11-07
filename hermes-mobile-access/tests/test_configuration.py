import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from configuration import (COLLECTION, SELECTOR, ConfigError, ConfigStore, draft,
                           new_id, parse_collection, missing_binding)


def collection(name="家里的 Mac", cid=None):
    return {"schema_version": 1, "connections": {cid or new_id(): draft(name)}}


def write(path, data, selector=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(COLLECTION + "=" + json.dumps(data, ensure_ascii=False) + "\n" +
                    (SELECTOR + "=" + selector + "\n" if selector else ""), encoding="utf-8")


@pytest.fixture
def setup(tmp_path):
    project, home = tmp_path / "project", tmp_path / "personal"
    project.mkdir()
    store = ConfigStore(project, home=home, use_local_key=True, environ={})
    return project, home, store


def test_collection_shadowing_empty_and_personal_opt_in(setup):
    project, home, store = setup
    lower = collection()
    cid = next(iter(lower["connections"]))
    write(home / ".env", lower, cid)
    assert store.snapshot().select()[0] == cid
    assert not ConfigStore(project, home=home, environ={}).snapshot().collection["connections"]
    write(project / ".env.local", {"schema_version": 1, "connections": {}})
    snap = store.snapshot()
    assert not snap.collection["connections"]
    with pytest.raises(ConfigError, match="请选择"):
        snap.select()
    (project / ".env.hermes-mobile-access").write_text(COLLECTION + "=\n")
    assert store.snapshot().source == str(project / ".env.local")


@pytest.mark.parametrize("raw", ["{", '{"schema_version":2,"connections":{}}',
    '{"schema_version":true,"connections":{}}', '{"schema_version":1,"connections":[],"secret":"do-not-echo"}',
    '{"schema_version":1,"connections":{},"connections":{}}'])
def test_malformed_high_layer_never_falls_back_or_echoes_values(setup, raw):
    project, home, store = setup
    write(home / ".env", collection())
    (project / ".env.local").write_text(COLLECTION + "=" + raw)
    with pytest.raises(ConfigError) as caught:
        store.snapshot()
    assert caught.value.source == str(project / ".env.local")
    assert "do-not-echo" not in str(caught.value)


def test_unicode_names_duplicate_ids_and_types_are_strict():
    data = collection(" CAFÉ ")
    data["connections"][new_id()] = draft("cafe\u0301")
    with pytest.raises(ConfigError) as caught:
        parse_collection(json.dumps(data), "source")
    assert caught.value.code == "duplicate_name"
    cid = new_id()
    raw = '{"schema_version":1,"connections":{"%s":{},"%s":{}}}' % (cid, cid)
    with pytest.raises(ConfigError) as caught:
        parse_collection(raw, "source")
    assert caught.value.code == "duplicate_key"
    data = collection()
    next(iter(data["connections"].values()))["auto_recover"] = "true"
    with pytest.raises(ConfigError):
        parse_collection(json.dumps(data), "source")


def test_roundtrip_rename_preserves_id_default_and_unrelated_dotenv(setup):
    project, home, store = setup
    home.mkdir()
    unrelated = b"# Keep this comment\r\nOTHER_KEY='a # b'\r\n"
    (home / ".env").write_bytes(unrelated)
    data = collection("我的 Mac")
    cid = next(iter(data["connections"]))
    saved = store.save(store.snapshot().revision, data, default_id=cid)
    data["connections"][cid]["name"] = "  办公 Mac  "
    saved = store.save(saved.revision, data)
    assert saved.select()[0] == cid
    assert saved.select()[1]["name"] == "办公 Mac"
    assert (home / ".env").read_bytes().startswith(unrelated)
    assert "auth" in missing_binding(saved.select()[1])


def test_stale_revision_and_environment_override_prevent_writes(setup):
    project, home, store = setup
    data = collection()
    snapshot = store.snapshot()
    write(project / ".env.local", collection("另一来源"))
    with pytest.raises(ConfigError) as caught:
        store.save(snapshot.revision, data)
    assert caught.value.code == "conflict"
    assert not (home / ".env").exists()
    store.environ[COLLECTION] = json.dumps(data)
    with pytest.raises(ConfigError) as caught:
        store.save(store.snapshot().revision, data)
    assert caught.value.code == "source_readonly"


def test_delete_default_leaves_explicit_empty_collection_and_blocks_active(setup):
    _, home, store = setup
    data = collection()
    cid = next(iter(data["connections"]))
    snap = store.save(store.snapshot().revision, data, default_id=cid)
    empty = {"schema_version": 1, "connections": {}}
    with pytest.raises(ConfigError) as caught:
        store.save(snap.revision, empty, active_ids=[cid])
    assert caught.value.code == "active_connection"
    result = store.save(snap.revision, empty)
    assert result.selector == "" and result.collection == empty
    assert COLLECTION in (home / ".env").read_text()


def test_no_parent_or_other_skill_scan(setup):
    project, home, store = setup
    write(project.parent / ".env", collection("parent"))
    write(project / ".env.other-skill", collection("other"))
    assert store.snapshot().source == ""


def test_no_default_never_selects_first(setup):
    _, home, store = setup
    write(home / ".env", collection())
    with pytest.raises(ConfigError) as caught:
        store.snapshot().select()
    assert caught.value.code == "selection_required"


@pytest.mark.parametrize('auth', [
    {}, {'type': 'basic', 'key': 'secret-must-not-echo'},
    {'type': 'mobile_key_v1', 'key': 'secret-must-not-echo'},
    {'type': 'mobile_key_v1', 'key': 'aaa••••••zzz'},
    {'type': 'mobile_key_v1', 'key': 'A' * 42 + 'B'},
    {'type': 'mobile_key_v1', 'key': 'A' * 43, 'extra': True},
])
def test_auth_schema_is_strict_and_errors_do_not_echo_values(auth):
    data = collection()
    next(iter(data['connections'].values()))['auth'] = auth
    with pytest.raises(ConfigError) as caught:
        parse_collection(json.dumps(data), 'fixture')
    assert 'secret-must-not-echo' not in json.dumps(caught.value.report())


def test_key_roundtrip_and_binding_change_detection(setup):
    from access_security import generate_key, binding_digest, valid_key
    _, _, store = setup
    data = collection()
    cid, record = next(iter(data['connections'].items()))
    key = generate_key()
    assert valid_key(key)
    record['auth'] = {'type': 'mobile_key_v1', 'key': key}
    saved = store.save(store.snapshot().revision, data, default_id=cid)
    assert store.snapshot().select()[1]['auth']['key'] == key
    original = binding_digest(saved.source, cid, record)
    record['name'] = 'renamed'
    record['auto_recover'] = False
    assert binding_digest(saved.source, cid, record) == original
    record['auth']['key'] = generate_key()
    assert binding_digest(saved.source, cid, record) != original
    record['auth'] = None
    store.save(saved.revision, data)
    assert store.snapshot().select()[1]['auth'] is None
