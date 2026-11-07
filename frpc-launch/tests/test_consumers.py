import argparse
import json
import os
import subprocess
import tomllib
import sys
import time

import pytest

import consumer_config as consumers
import project_config as projects
from test_project_lifecycle import setup  # existing native fake client fixture


def test_owner_exit_stops_only_its_consumer(independent):
    home, project, run, configure, run_project = independent
    # A real short-lived parent process, without any FRPS connection.
    owner = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read(1)'],
                             stdin=subprocess.PIPE, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        for instance in ('first', 'second'):
            assert configure(instance, 3001)[0] == 0
        assert run('first', 'start', '--wait', '5', '--owner-pid', str(owner.pid))[0] == 0
        same_code, same = run('first', 'start', '--wait', '5', '--owner-pid', str(owner.pid))
        assert same_code == 0 and same['result'] == 'already_running'
        for arguments in ((), ('--owner-pid', str(os.getpid()))):
            code, changed = run('first', 'start', '--wait', '5', *arguments)
            assert code != 0 and changed['result'] == 'owner_changed'
        assert run('second', 'start', '--wait', '5')[0] == 0
        owner.stdin.close()
        owner.wait(5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not run('first', 'status')[1]['modes']['official']['running']:
                break
            time.sleep(.2)
        assert not run('first', 'status')[1]['modes']['official']['running']
        assert run('second', 'status')[1]['modes']['official']['running']
    finally:
        if owner.poll() is None:
            owner.terminate()
            owner.wait(5)


def test_consumer_tls_requirement_rejects_explicit_disable(tmp_path):
    project, home = tmp_path / 'project', tmp_path / 'home'
    project.mkdir()
    home.mkdir()
    config = home / 'frpc.toml'
    config.write_text('serverAddr="frp.example.test"\nserverPort=7005\nauth.token="fixture"\ntransport.tls.enable=false\n')
    args = argparse.Namespace(project=project, home=home, consumer='hermes-mobile-access', instance='tls',
        domain_prefix='mobile', local_ip='127.0.0.1', local_port=3000, proxy_type='http', locations=[], require_tls=True)
    report = consumers.context_report({}, project, home)
    projects.atomic_write(consumers.route_path(args), json.dumps(consumers.make_route(args, report)))
    with pytest.raises(projects.ProjectError) as caught:
        consumers.effective_config(args, {})
    assert caught.value.code == 'tls_required'
    config.write_text(config.read_text().replace('enable=false', 'enable=true'))
    report = consumers.context_report({}, project, home)
    projects.atomic_write(consumers.route_path(args), json.dumps(consumers.make_route(args, report)))
    assert tomllib.loads(consumers.effective_config(args, {})[0])['transport']['tls']['enable'] is True


def test_context_reports_suffix_even_when_connection_is_incomplete(tmp_path):
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    home.mkdir()
    (project / ".env.frpc-launch").write_text("FRPC_LAUNCH_DOMAIN_SUFFIX=team.example.test\nFRPC_LAUNCH_TOKEN=incomplete-secret\n")
    result = consumers.context_report({}, project, home)
    assert result["domain_suffix"]["value"] == "team.example.test"
    assert result["connection"]["error"]["result"] == "incomplete_connection"
    assert "incomplete-secret" not in json.dumps(result)
    assert result["domain_suffix"]["source"] == str(project / ".env.frpc-launch")
    (project / ".env.frpc-launch").write_text("FRPC_LAUNCH_DOMAIN_SUFFIX=https://wrong.example/\n")
    invalid = consumers.context_report({}, project, home)
    assert invalid["domain_suffix"]["status"] == "invalid"
    assert invalid["domain_suffix"]["source"] == str(project / ".env.frpc-launch")


def test_context_revision_detects_credential_and_source_changes(tmp_path):
    tmp_path.joinpath(".env").write_text("FRPC_LAUNCH_SERVER_ADDR=a.test\nFRPC_LAUNCH_SERVER_PORT=7000\nFRPC_LAUNCH_TOKEN=one\nFRPC_LAUNCH_DOMAIN_SUFFIX=a.test\n")
    before = consumers.context_report({}, tmp_path, tmp_path / "missing")
    after = consumers.context_report({"FRPC_LAUNCH_DOMAIN_SUFFIX": "a.test"}, tmp_path, tmp_path / "missing")
    assert before["revision"] != after["revision"]
    text = tmp_path.joinpath(".env").read_text().replace("TOKEN=one", "TOKEN=two")
    tmp_path.joinpath(".env").write_text(text)
    assert before["revision"] != consumers.context_report({}, tmp_path, tmp_path / "missing")["revision"]


def test_native_connection_host_supplies_consumer_parent_without_extra_setting(tmp_path):
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    home.mkdir()
    config = home / "frpc.toml"
    config.write_text('serverAddr="devfrp.example.test"\nserverPort=7005\nauth.token="source-secret"\n')
    before = config.read_bytes()
    context = consumers.context_report({}, project, home)
    assert context["domain_suffix"] == {
        "value": "devfrp.example.test", "source": str(config), "status": "configured",
        "basis": "server_addr", "verification": "configuration_only"}
    args = argparse.Namespace(project=project, home=home, consumer="hermes-mobile-access", instance="hma_test",
        domain_prefix="hermes-mobile", local_ip="127.0.0.1", local_port=1234, proxy_type="http", locations=[])
    route = consumers.make_route(args, context)
    projects.atomic_write(consumers.route_path(args), json.dumps(route))
    effective, _, _ = consumers.effective_config(args, {})
    parsed = tomllib.loads(effective)
    assert route["domain"] == "hermes-mobile.devfrp.example.test"
    assert parsed["serverPort"] == 7005
    assert parsed["proxies"][0]["customDomains"] == [route["domain"]]
    assert "source-secret" not in json.dumps(context) + json.dumps(route)
    assert config.read_bytes() == before
    config.write_text(config.read_text().replace("devfrp.example.test", "changed.example.test"))
    with pytest.raises(projects.ProjectError) as caught:
        consumers.effective_config(args, {})
    assert caught.value.code == "context_changed"


def test_consumer_parent_uses_selected_connection_and_preserves_explicit_suffix(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "frpc.toml").write_text('serverAddr="lower.example.test"\nserverPort=7005\nauth.token="lower-secret"\n')
    env = {"FRPC_LAUNCH_SERVER_ADDR": "selected.example.test", "FRPC_LAUNCH_SERVER_PORT": "7000",
           "FRPC_LAUNCH_TOKEN": "selected-secret"}
    context = consumers.context_report(env, tmp_path, home)
    assert context["domain_suffix"]["value"] == "selected.example.test"
    assert context["domain_suffix"]["source"] == "env"
    explicit = consumers.context_report(dict(env, FRPC_LAUNCH_DOMAIN_SUFFIX="service.example.test"), tmp_path, home)
    assert explicit["domain_suffix"]["value"] == "service.example.test"
    assert explicit["domain_suffix"]["basis"] == "domain_suffix_setting"
    invalid = consumers.context_report(dict(env, FRPC_LAUNCH_DOMAIN_SUFFIX="https://invalid.test/"), tmp_path, home)
    assert invalid["domain_suffix"]["status"] == "invalid"
    incomplete = consumers.context_report({"FRPC_LAUNCH_TOKEN": "partial"}, tmp_path, home)
    assert incomplete["connection"]["error"]["result"] == "incomplete_connection"
    assert incomplete["domain_suffix"]["status"] == "missing"


@pytest.mark.parametrize("addr", ["127.0.0.1", "::1", "[::1]", "https://invalid.test/"])
def test_consumer_parent_never_constructed_from_ip_or_url(tmp_path, addr):
    context = consumers.context_report({"FRPC_LAUNCH_SERVER_ADDR": addr, "FRPC_LAUNCH_SERVER_PORT": "7000",
                                        "FRPC_LAUNCH_TOKEN": "fixture"}, tmp_path, tmp_path / "home")
    assert context["connection"]["status"] == "configured"
    assert context["domain_suffix"]["value"] == ""
    assert context["domain_suffix"]["status"] == "missing"


def test_effective_config_preserves_source_paths_and_excludes_other_routes(tmp_path):
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    home.mkdir()
    (home / ".env").write_text("FRPC_LAUNCH_DOMAIN_SUFFIX=example.test\n")
    (home / "frpc.toml").write_text('serverAddr="a.test"\nserverPort=7000\nauth.token="source-secret"\ntransport.tls.trustedCaFile="ca.pem"\n[[proxies]]\nname="other-global"\ntype="tcp"\nlocalPort=3000\nremotePort=3000\n')
    args = argparse.Namespace(project=project, home=home, consumer="hermes-mobile-access", instance="hma_test",
        domain_prefix="hermes-mobile", local_ip="127.0.0.1", local_port=1234, proxy_type="http", locations=[])
    report = consumers.context_report({}, project, home)
    route = consumers.make_route(args, report)
    projects.atomic_write(consumers.route_path(args), json.dumps(route))
    config, connection, _ = consumers.effective_config(args, {})
    parsed = tomllib.loads(config)
    assert connection.source == str(home / "frpc.toml")
    assert parsed["transport"]["tls"]["trustedCaFile"] == str(home / "ca.pem")
    assert parsed["proxies"] == [route["proxy"]]
    assert parsed["auth"]["token"] == "source-secret"
    route["domain"] = "hermes-mobile.unrelated.test"
    route["proxy"]["customDomains"] = [route["domain"]]
    projects.atomic_write(consumers.route_path(args), json.dumps(route))
    with pytest.raises(projects.ProjectError) as caught:
        consumers.effective_config(args, {})
    assert caught.value.code == "context_changed"
    route["proxy"]["httpPassword"] = "must-not-leak"
    projects.atomic_write(consumers.route_path(args), json.dumps(route))
    with pytest.raises(projects.ProjectError) as caught:
        consumers.load_route(args)
    assert caught.value.code == "invalid_route"
    assert "must-not-leak" not in str(caught.value)


@pytest.fixture
def independent(setup):
    home, (project, _), env, command, run_project = setup
    def run(instance, *args, check=True):
        argv = command(project, *args)
        argv[2:2] = ["--consumer", "hermes-mobile-access", "--instance", instance]
        process = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
        if check:
            assert process.stdout, process.stderr
        return process.returncode, json.loads(process.stdout) if process.stdout else process.stderr
    def configure(instance, port):
        context = run(instance, "context-info")[1]
        return run(instance, "instance-config", "--context-revision", context["revision"],
                   "--domain-prefix", instance, "--local-port", str(port))
    yield home, project, run, configure, run_project
    for instance in ("first", "second"):
        run(instance, "stop", check=False)


def test_two_instances_reuse_project_source_and_stop_without_credentials(independent):
    home, project, run, configure, run_project = independent
    original = projects.config_path(project).read_bytes()
    assert configure("first", 3001)[0] == 0
    assert configure("second", 3002)[0] == 0
    assert run_project(project, "start", "--wait", "5")[0] == 0
    a = run("first", "start", "--wait", "5")[1]
    b = run("second", "start", "--wait", "5")[1]
    assert a["result"] == b["result"] == "ok"
    assert a["pid"] != b["pid"]
    assert a["proxy"]["localPort"] == 3001 and b["proxy"]["localPort"] == 3002
    assert a["proxy"]["name"] != b["proxy"]["name"]
    assert run("first", "start", "--wait", "5")[1]["pid"] == a["pid"]
    assert projects.config_path(project).read_bytes() == original
    (home / ".env").write_text("FRPC_LAUNCH_TOKEN=incomplete\n")
    assert run("first", "status")[1]["modes"]["official"]["running"]
    assert run("first", "stop")[0] == 0
    assert not run("first", "status")[1]["modes"]["official"]["running"]
    assert run("second", "status")[1]["modes"]["official"]["running"]
    assert run_project(project, "status")[1]["modes"]["official"]["running"]


def test_running_route_and_stale_context_cannot_be_replaced(independent):
    home, project, run, configure, _ = independent
    context = run("first", "context-info")[1]
    assert configure("first", 3001)[0] == 0
    assert run("first", "start", "--wait", "5")[0] == 0
    assert configure("first", 3002)[1]["result"] == "running"
    with (home / ".env").open("a") as stream:
        stream.write("FRPC_LAUNCH_DOMAIN_SUFFIX=changed.example.test\n")
    code, report = run("first", "instance-config", "--context-revision", context["revision"],
                       "--domain-prefix", "first", "--local-port", "3002")
    assert code == 5 and report["result"] == "context_changed"
    assert run("first", "start", "--wait", "5")[1]["result"] == "context_changed"
    assert run("first", "status")[1]["modes"]["official"]["running"]


def test_foreign_pid_record_does_not_stop_another_instance(independent):
    home, project, run, configure, _ = independent
    for instance in ("first", "second"):
        assert configure(instance, 3000)[0] == 0
        assert run(instance, "start", "--wait", "5")[0] == 0
    first = home / "consumers/hermes-mobile-access/first/run/official.pid"
    second = home / "consumers/hermes-mobile-access/second/run/official.pid"
    original = first.read_bytes()
    try:
        first.write_bytes(second.read_bytes())
        assert run("first", "stop")[1]["modes"]["official"]["result"] == "stale_pid_cleaned"
        assert run("second", "status")[1]["modes"]["official"]["running"]
    finally:
        first.write_bytes(original)
