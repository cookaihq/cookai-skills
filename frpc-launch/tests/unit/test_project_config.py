import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomlkit

import project_config as config


def group(host="one.example.test", token="synthetic-token-one"):
    return dict(zip(config.CONNECTION_KEYS, (host, "7000", token)))


def save_env(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.updated_env("", values), encoding="utf-8")


@pytest.fixture
def places(tmp_path):
    project, defaults = tmp_path / "project", tmp_path / "defaults"
    project.mkdir()
    defaults.mkdir()
    return project, defaults


def test_atomic_connection_precedence_and_sources(places):
    project, defaults = places
    save_env(defaults / ".env", group("global.example.test"))
    save_env(project / ".env", group("dotenv.example.test"))
    save_env(project / ".env.local", group("project.example.test"))
    save_env(project / ".env.frpc-launch", group("dedicated.example.test"))
    for environment, request, expected in [
        ({}, None, "dedicated.example.test"),
        (group("env.example.test"), None, "env.example.test"),
        (group("env.example.test"), group("request.example.test"), "request.example.test"),
    ]:
        selected = config.resolve_connection(environment, project, defaults, request)
        assert selected.addr == expected
        assert "synthetic-token" not in json.dumps(selected.report())
        assert all(field["source"] == selected.source for field in selected.report()["fields"].values())
    (project / ".env.frpc-launch").unlink()
    selected = config.resolve_connection({}, project, defaults)
    assert selected.addr == "project.example.test"
    assert selected.source == str(project / ".env.local")
    (project / ".env.local").unlink()
    (project / ".env").unlink()
    assert config.resolve_connection({}, project, defaults).addr == "global.example.test"


@pytest.mark.parametrize("location", ["request", "env", ".env.frpc-launch", ".env.local", ".env", "global"])
def test_incomplete_connection_never_borrows_another_token(places, location):
    project, defaults = places
    (defaults / "frpc.toml").write_text(
        'serverAddr="other.example.test"\nserverPort=7000\nauth.token="other-synthetic-token"\n')
    partial = {config.CONNECTION_KEYS[0]: "new.example.test"}
    environment, request = {}, None
    if location == "request":
        request = partial
    elif location == "env":
        environment = partial
    else:
        save_env((defaults / ".env") if location == "global" else project / location, partial)
    with pytest.raises(config.ProjectError) as error:
        config.resolve_connection(environment, project, defaults, request)
    assert error.value.code == "incomplete_connection"
    assert config.CONNECTION_KEYS[2] in error.value.missing
    assert "other-synthetic-token" not in json.dumps(error.value.report())


def test_no_parent_search_or_other_skill_read(places):
    project, defaults = places
    save_env(project.parent / ".env.local", group())
    save_env(project / ".env.some-other-skill", group())
    with pytest.raises(config.ProjectError, match="未找到"):
        config.resolve_connection({}, project, defaults)


def test_unreadable_high_layer_does_not_silently_select_global(places):
    project, defaults = places
    (project / ".env.local").write_bytes(b"\xffbad")
    save_env(defaults / ".env", group())
    with pytest.raises(config.ProjectError, match="无法读取"):
        config.resolve_connection({}, project, defaults)


def test_selected_group_does_not_read_broken_lower_layer(places):
    project, defaults = places
    save_env(project / ".env.local", group())
    (defaults / "frpc.toml").write_bytes(b"\xffbad")
    assert config.resolve_connection({}, project, defaults).addr == "one.example.test"


def test_global_toml_supplies_connection_without_other_projects_proxies(places):
    project, defaults = places
    (defaults / "frpc.toml").write_text(
        'serverAddr="global.example.test"\nserverPort=7000\nauth.token="fake-token-global"\n'
        'auth.additionalScopes=["HeartBeats", "NewWorkConns"]\n'
        'transport.tls.enable=true\n[[proxies]]\nname="unrelated"\ntype="tcp"\n'
        'localPort=1234\nremotePort=4321\n')
    selected = config.resolve_connection({}, project, defaults)
    document = config.load_project(project)
    config.upsert_proxies(document, [{"name": "current", "type": "http", "localPort": 3000,
                                      "customDomains": ["project.dev.example.test"]}])
    rendered = tomlkit.parse(config.effective_config(selected, document))
    assert [p["name"] for p in rendered["proxies"]] == ["current"]
    assert rendered["transport"]["tls"]["enable"] is True
    assert rendered["auth"]["token"] == "fake-token-global"
    assert rendered["auth"]["additionalScopes"] == ["HeartBeats", "NewWorkConns"]
    assert selected.source == str(defaults / "frpc.toml")


def test_saved_connection_needs_no_local_port_and_survives_next_call(places):
    project, defaults = places
    selected = config.resolve_connection(group(), project, defaults)
    document = config.load_project(project)
    config.save_project(project, defaults, selected, document, {}, persist_connection=True)
    reloaded = config.resolve_connection({}, project, defaults)
    assert reloaded.addr == selected.addr and reloaded.token == selected.token
    assert reloaded.source == str(project / ".env.local")
    assert not (defaults / ".env").exists() and not (defaults / "frpc.toml").exists()
    assert selected.token not in config.config_path(project).read_text()
    with pytest.raises(config.ProjectError) as error:
        config.effective_config(reloaded, config.load_project(project))
    assert error.value.code == "no_project_proxy"


def test_duplicate_env_keys_are_replaced_without_losing_other_settings():
    original = 'OTHER=keep\nFRPC_LAUNCH_TOKEN=old\n# comment\nFRPC_LAUNCH_TOKEN=later-old\n'
    updated = config.updated_env(original, {"FRPC_LAUNCH_TOKEN": '"test-value"'})
    assert config.parse_env(updated)["FRPC_LAUNCH_TOKEN"] == '"test-value"'
    assert "OTHER=keep" in updated and "# comment" in updated
    assert "old" not in updated


def test_fixed_domain_and_actual_local_name_collision(tmp_path):
    a, b = tmp_path / "a" / "Demo App", tmp_path / "b" / "Demo App"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    registry = {}
    da, db = config.load_project(a), config.load_project(b)
    first = config.select_domain(da, a, "dev.example.test", "", registry)
    second = config.select_domain(db, b, "dev.example.test", "", registry)
    assert first == "demo-app.dev.example.test"
    assert second != first and second.endswith(".dev.example.test")
    assert config.select_domain(da, a, "new.example.test", "", registry) == first
    with pytest.raises(config.ProjectError) as error:
        config.select_domain(db, b, "", first, registry, replace=True)
    assert error.value.code == "domain_conflict"
    assert config.runtime_home(a, tmp_path) != config.runtime_home(b, tmp_path)


def test_explicit_domain_change_requires_deliberate_replacement(places):
    project, _ = places
    document, registry = config.load_project(project), {}
    original = config.select_domain(document, project, "dev.example.test", "", registry)
    with pytest.raises(config.ProjectError) as error:
        config.select_domain(document, project, "", "chosen.example.test", registry)
    assert error.value.code == "domain_fixed"
    assert config.select_domain(document, project, "", "chosen.example.test", registry, True) == "chosen.example.test"
    assert original not in registry


@pytest.mark.parametrize("domain", ["https://x.example.test", "*.example.test", "x.example.test:443", "x..test", "a" * 64 + ".test"])
def test_invalid_domains_fail(domain):
    with pytest.raises(config.ProjectError):
        config.validate_domain(domain)


def test_upsert_preserves_routes_advanced_fields_and_comments(places):
    project, _ = places
    path = config.config_path(project)
    path.parent.mkdir()
    path.write_text('schema=1\n# keep me\n[client.transport]\nprotocol="quic"\n'
                    '[[client.proxies]]\nname="web"\ntype="http"\nlocalPort=3000\n'
                    'customDomains=["app.example.test"]\nlocations=["/api/"]\n'
                    '[client.proxies.transport]\nuseEncryption=true\n'
                    '[[client.proxies]]\nname="other"\ntype="tcp"\nlocalPort=22\nremotePort=9000\n')
    document = config.load_project(project)
    config.upsert_proxies(document, [{"name": "web", "type": "http", "localPort": 4000}])
    result = document["client"]
    assert result["proxies"][0]["localPort"] == 4000
    assert result["proxies"][0]["locations"] == ["/api/"]
    assert result["proxies"][0]["transport"]["useEncryption"] is True
    assert result["proxies"][1]["remotePort"] == 9000
    assert result["transport"]["protocol"] == "quic"
    assert "# keep me" in tomlkit.dumps(document)


def test_second_file_failure_restores_previous_project_configuration(places, monkeypatch):
    project, defaults = places
    save_env(project / ".env.local", group("old.example.test", "old-synthetic-token"))
    selected = config.resolve_connection(group("new.example.test"), project, defaults)
    previous = (project / ".env.local").read_bytes()
    original_write = config.atomic_write
    def fail_project(path, text):
        if path == config.config_path(project):
            raise OSError("synthetic disk failure")
        original_write(path, text)
    monkeypatch.setattr(config, "atomic_write", fail_project)
    with pytest.raises(OSError):
        config.save_project(project, defaults, selected, config.load_project(project), {}, True)
    assert (project / ".env.local").read_bytes() == previous
    assert not config.config_path(project).exists()
    assert not (defaults / "domains.json").exists()


def test_git_guard_reads_existing_repository_without_creating_branches():
    # Use the existing checkout, read-only. No git init, commit or index changes.
    root = Path(__file__).resolve().parents[3]
    if subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                      capture_output=True).returncode:
        pytest.skip("This fixture requires the existing checkout, not an exported container copy.")
    with pytest.raises(config.ProjectError) as error:
        config.secret_target_check(root, root / "README.md")
    assert error.value.exit_code == 6
    with pytest.raises(config.ProjectError) as error:
        config.secret_target_check(root, root / "frpc-launch-synthetic-unignored-target")
    assert error.value.exit_code == 6
    ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", ".env.local"], capture_output=True)
    assert ignored.returncode == 0
    config.secret_target_check(root, root / ".env.local")


def options(places, **overrides):
    project, defaults = places
    values = dict(project=project, home=defaults, server_addr="", server_port=0,
                  domain_suffix="", domain="", replace_domain=False, local_port=None,
                  proxy_name="", local_ip="", proxy_type="", route_mode="",
                  subdomain_host="", locations=None, proxies=[])
    values.update(overrides)
    return SimpleNamespace(**values)


def test_configure_persists_explicit_input_and_does_not_require_a_port(places):
    from frpc_launch import parse_proxy_spec
    project, defaults = places
    save_env(defaults / ".env", group("global.example.test", "global-synthetic-token"))
    previous = (defaults / ".env").read_bytes()
    result = config.configure(options(places, server_addr="new.example.test", server_port=8000),
                              {"FRPC_LAUNCH_INIT_TOKEN": "new-synthetic-token"}, parse_proxy_spec)
    assert result["connection_saved"] is True
    assert result["config_source"] == str(project / ".env.local")
    assert result["proxies"] == [] and result["domain"] == ""
    assert (defaults / ".env").read_bytes() == previous
    selected = config.resolve_connection({}, project, defaults)
    assert (selected.addr, selected.port, selected.token) == ("new.example.test", 8000, "new-synthetic-token")


def test_global_fallback_creates_only_project_routes_not_project_secret_copy(places):
    from frpc_launch import parse_proxy_spec
    project, defaults = places
    save_env(defaults / ".env", {**group(), config.SUFFIX_KEY: "dev.example.test"})
    result = config.configure(options(places, local_port=3000, route_mode="customDomains"), {}, parse_proxy_spec)
    assert result["connection_saved"] is False and result["uses_global_connection"] is True
    assert result["domain"] == "project.dev.example.test"
    assert result["proxies"][0]["customDomains"] == [result["domain"]]
    assert result["proxies"][0]["type"] == "http"
    assert not (project / ".env.local").exists()
    assert "synthetic-token" not in config.config_path(project).read_text()


def test_next_call_reuses_domain_and_preserves_unspecified_path(places):
    from frpc_launch import parse_proxy_spec
    project, defaults = places
    save_env(defaults / ".env", {**group(), config.SUFFIX_KEY: "dev.example.test"})
    config.configure(options(places, local_port=3000, route_mode="customDomains", locations=["/api/"]), {}, parse_proxy_spec)
    save_env(defaults / ".env", {**group(), config.SUFFIX_KEY: "changed.example.test"})
    result = config.configure(options(places, local_port=4000), {}, parse_proxy_spec)
    assert result["domain"] == "project.dev.example.test"
    assert result["proxies"][0]["localPort"] == 4000
    assert result["proxies"][0]["locations"] == ["/api/"]


def test_incomplete_explicit_request_writes_nothing(places):
    from frpc_launch import parse_proxy_spec
    project, defaults = places
    save_env(defaults / ".env", group())
    with pytest.raises(config.ProjectError) as error:
        config.configure(options(places, server_addr="new.example.test"), {}, parse_proxy_spec)
    assert error.value.code == "incomplete_connection"
    assert not config.config_path(project).exists()
    assert not (project / ".env.local").exists()


def test_conflicting_higher_priority_project_file_blocks_ineffective_save(places):
    from frpc_launch import parse_proxy_spec
    project, _ = places
    save_env(project / ".env.frpc-launch", group())
    with pytest.raises(config.ProjectError) as error:
        config.configure(options(places, server_addr="new.example.test", server_port=8000),
                         {"FRPC_LAUNCH_INIT_TOKEN": "new-synthetic-token"}, parse_proxy_spec)
    assert error.value.code == "save_shadowed"
    assert not (project / ".env.local").exists()
    assert not config.config_path(project).exists()


def test_subdomain_requires_actual_matching_frps_host(places):
    project, _ = places
    document = config.load_project(project)
    document["domain"] = "project.dev.example.test"
    with pytest.raises(config.ProjectError) as error:
        config.local_proxy(document, project, 3000, route_mode="subdomain")
    assert error.value.code == "subdomain_host_unconfirmed"
    result = config.local_proxy(document, project, 3000, route_mode="subdomain", subdomain_host="dev.example.test")
    assert result["subdomain"] == "project" and "customDomains" not in result


def test_first_route_is_not_inferred_from_https_or_domain(places):
    project, _ = places
    document = config.load_project(project)
    document["domain"] = "project.dev.example.test"
    with pytest.raises(config.ProjectError) as error:
        config.local_proxy(document, project, 3000)
    assert error.value.code == "route_unconfirmed"


def test_template_is_not_a_second_persistent_secret_store(places):
    project, defaults = places
    selected = config.resolve_connection(group(), project, defaults)
    document = config.load_project(project)
    config.upsert_proxies(document, [{"name": "a", "type": "http", "localPort": 3000,
                                      "customDomains": ["a.example.test"]}])
    text = config.effective_config(selected, document)
    assert selected.token in text  # in memory only, passed via the child environment
    assert selected.token not in config.RUNTIME_TEMPLATE
    assert selected.token not in tomlkit.dumps(document)


def test_independent_proxy_names_even_when_projects_have_same_directory_name(tmp_path):
    a, b = tmp_path / "a" / "same", tmp_path / "b" / "same"
    assert config.default_proxy_name(a) != config.default_proxy_name(b)


def test_project_sakura_cannot_bypass_secret_guard(places, monkeypatch):
    from frpc_launch import build_parser, cmd_guide_init
    project, defaults = places
    monkeypatch.setenv("FRPC_LAUNCH_SAKURA_KEY", "synthetic-sakura-key")
    monkeypatch.setenv("FRPC_LAUNCH_SAKURA_TUNNELS", "123")
    def refuse(current, target):
        assert current == project and target == project / ".env.local"
        raise config.ProjectError("synthetic unsafe target", exit_code=6)
    monkeypatch.setattr(config, "secret_target_check", refuse)
    args = build_parser().parse_args(["--project", str(project), "--home", str(defaults),
                                     "guide-init", "--source", "sakura", "--scope", "project",
                                     "--allow-tracked"])
    with pytest.raises(config.ProjectError) as error:
        cmd_guide_init(args)
    assert error.value.exit_code == 6
    assert not (project / ".env.local").exists()
