import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "assets" / "runtime"))
sys.path.insert(0, str(ROOT / "examples"))
from skill_ui import UIError
from skill_ui.server import Server
from skill_ui.storage import JsonConfig
from studio_adapter import Studio
from layers_adapter import LayeredStudio

TMP = Path(os.environ.get("SKILL_UI_TEST_TMP", Path.cwd() / "tmp" / "skill-ui-tests")).resolve()
TMP.mkdir(parents=True, exist_ok=True)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TMP)
        self.project = Path(self.tmp.name)
        self.servers = []

    def tearDown(self):
        for server, thread in self.servers:
            server.shutdown()
            thread.join(3)
            server.closed.set()
            server.server_close()
        self.tmp.cleanup()

    def server(self, page, adapter_class=Studio, **kwargs):
        server = Server(adapter_class(self.project), self.project, page, self.project, **kwargs)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval":0.05}, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))
        return server, opener

    def request(self, server, opener, path, data=None, headers=None, decode=True):
        base = {"Origin":server.origin, "Content-Type":"application/json", "X-CSRF-Token":server.csrf}
        req = Request(server.origin+path, data=json.dumps(data).encode() if data is not None else None, headers={**base, **(headers or {})})
        try:
            response = opener.open(req, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            return response.status, json.loads(raw) if decode and raw else raw, response.headers

    def login(self, server, opener):
        self.assertEqual(self.request(server,opener,"/api/session",{"token":server.token})[0],200)

    def test_missing_config_boots_mask_reveal_save_conflict(self):
        server, opener = self.server("configuration")
        self.assertEqual(self.request(server,opener,"/api/state")[0],401)
        self.login(server,opener)
        state = self.request(server,opener,"/api/state")[1]
        self.assertFalse(state["config"]["fields"][0]["effective"]["configured"])
        first = state["config"]["revision"]
        secret = "demo-key-middle-private-987654"
        code, saved, _ = self.request(server,opener,"/api/save",{"target":"project","revision":first,"changes":{"api_key":{"op":"set","value":secret}}})
        self.assertEqual(code,200)
        self.assertNotIn(secret,json.dumps(saved))
        self.assertNotIn("middle-private",json.dumps(saved))
        self.assertNotIn(secret,(self.project/"saved.json").read_text(encoding="utf-8"))
        current = saved["config"]["revision"]
        self.assertEqual(self.request(server,opener,"/api/reveal",{"revision":current,"id":"api_key"})[1]["value"],secret)
        self.assertEqual(self.request(server,opener,"/api/save",{"target":"project","revision":first,"changes":{"format":{"op":"set","value":"纯文本"}}})[0],409)
        self.assertEqual(json.loads((self.project/"demo-state/settings.json").read_text(encoding="utf-8"))["api_key"],secret)
        self.assertEqual(self.request(server,opener,"/api/reveal",{"revision":first,"id":"api_key"})[0],409)

    def test_validation_atomic_unknown_preserved_defaults_and_order(self):
        path=self.project/"config.json"
        path.write_text(json.dumps({"unknown":"keep","key":"existing","ordered":["b","a"]}))
        store=JsonConfig(path,[{"id":"key","label":"Key","sensitive":True,"required":True},
                              {"id":"ordered","label":"顺序","type":"list","default":["c"]},
                              {"id":"enabled","label":"开启","type":"boolean","default":True},
                              {"id":"count","label":"数量","type":"number","depends_on":{"enabled":True}}])
        revision=store.snapshot()["revision"]
        old=path.read_bytes()
        for changes in ({"key":{"op":"unset"}}, {"count":{"op":"set","value":True}},
                        {"enabled":{"op":"set","value":False},"count":{"op":"set","value":3}}):
            with self.assertRaises(UIError):store.save(changes,revision,"project")
            self.assertEqual(path.read_bytes(),old)
        store.save({"ordered":{"op":"set","value":["z","a"]}},revision,"project")
        self.assertEqual(json.loads(path.read_text()),{"unknown":"keep","key":"existing","ordered":["z","a"]})
        result=store.save({"ordered":{"op":"unset"}},store.snapshot()["revision"],"project")
        self.assertEqual(result["fields"][1]["effective"]["value"],["c"])
        self.assertEqual(result["fields"][1]["effective"]["source"],"default")

    def test_layer_preview_masking_and_exact_target_save(self):
        server, opener = self.server("configuration", adapter_class=LayeredStudio)
        self.login(server, opener)
        state = self.request(server, opener, "/api/state")[1]["config"]
        request = {"target":"project_local", "revision":state["revision"], "changes":{"api_key":{"op":"unset"}}}
        files = {p:p.read_bytes() for p in server.adapter.store.directory.glob("*.json")}
        code, preview, _ = self.request(server, opener, "/api/preview", request)
        self.assertEqual(code, 200)
        key = next(f for f in preview["config"]["fields"] if f["id"] == "api_key")
        self.assertEqual(key["effective"]["source"], "plugin_env")
        self.assertNotIn("value", key["effective"])
        self.assertTrue(all("value" not in row for row in key["rows"]))
        self.assertEqual(files, {p:p.read_bytes() for p in files})
        for target, expected in ((None,"demo-project-key-0002"),("plugin_env","demo-global-key-0001")):
            self.assertEqual(self.request(server, opener, "/api/reveal", {"id":"api_key", "target":target, "revision":state["revision"]})[1]["value"], expected)
        self.assertEqual(self.request(server, opener, "/api/reveal", {"id":"api_key", "target":"../bad", "revision":state["revision"]})[0], 400)
        code, saved, _ = self.request(server, opener, "/api/save", request)
        self.assertEqual(code, 200)
        self.assertEqual(saved["receipt"]["target"], "project_local")
        self.assertEqual(files[server.adapter.store.path("plugin_env")], server.adapter.store.path("plugin_env").read_bytes())
        self.assertEqual(self.request(server, opener, "/api/preview", request)[0], 409)
        self.assertEqual(self.request(server, opener, "/configuration.js", decode=False)[0], 200)

    def test_auth_origin_csrf_and_page_boundaries(self):
        config,cookies=self.server("configuration")
        artifacts,other=self.server("artifacts")
        self.login(artifacts,other)
        self.assertEqual(self.request(config,other,"/api/state")[0],401)
        self.login(config,cookies)
        self.assertEqual(self.request(config,cookies,"/api/residency",{"resident":True})[0],400)
        self.assertEqual(self.request(config,cookies,"/resources/document")[0],404)
        self.assertEqual(self.request(artifacts,other,"/api/reveal",{"id":"api_key"})[0],404)
        self.assertEqual(self.request(config,cookies,"/api/activity",{},headers={"X-CSRF-Token":"wrong"})[0],403)
        self.assertEqual(self.request(config,cookies,"/api/activity",{},headers={"Origin":"https://bad.example"})[0],403)
        self.assertEqual(self.request(config,cookies,"/api/state",headers={"Host":"bad.example"})[0],403)

    def test_independent_lifetime_polling_resident_cancel_and_stop(self):
        config,co=self.server("configuration",idle_seconds=60)
        artifacts,ar=self.server("artifacts",idle_seconds=60)
        self.login(config,co); self.login(artifacts,ar)
        config.last_use-=20; artifacts.last_use-=20
        previous=config.last_use
        self.request(config,co,"/api/state");self.request(config,co,"/api/lifetime")
        self.assertEqual(config.last_use,previous)
        self.request(artifacts,ar,"/api/residency",{"resident":True})
        artifacts.last_use-=1000;config.last_use-=1000
        self.assertTrue(self.request(artifacts,ar,"/api/state")[1]["lifetime"]["resident"])
        self.assertEqual(self.request(config,co,"/api/activity",{})[0],410)
        cancelled=self.request(artifacts,ar,"/api/residency",{"resident":False})[1]
        self.assertFalse(cancelled["resident"]);self.assertEqual(cancelled["remaining"],60)
        self.request(artifacts,ar,"/api/stop",{})
        with self.assertRaises(UIError):artifacts.lifetime()

    def test_download_range_whitelist_and_custom_renderer(self):
        server,opener=self.server("artifacts")
        self.login(server,opener)
        state=self.request(server,opener,"/api/state")[1]
        self.assertEqual(len(state["artifacts"]["items"]),6)
        code,data,headers=self.request(server,opener,"/resources/audio",headers={"Range":"bytes=0-43"},decode=False)
        self.assertEqual(code,206);self.assertEqual(data[:4],b"RIFF");self.assertEqual(len(data),44)
        self.assertEqual(self.request(server,opener,"/resources/audio",headers={"Range":"bytes=999999-"},decode=False)[0],416)
        self.assertEqual(self.request(server,opener,"/resources/%2e%2e%2fsettings.json")[0],404)
        code,data,headers=self.request(server,opener,"/resources/cover?download=1",decode=False)
        self.assertIn("attachment",headers["Content-Disposition"]);self.assertIn("sandbox",headers["Content-Security-Policy"])
        self.assertEqual(self.request(server,opener,"/extensions/receipt-view.js",decode=False)[0],200)
        self.assertEqual(self.request(server,opener,"/extensions/../studio_adapter.py")[0],404)

    def test_vendor_reintegration_and_customizations(self):
        target=self.project/"business/shared/skill_ui"
        command=[sys.executable,str(ROOT/"scripts/vendor.py"),"--target",str(target)]
        first=subprocess.run(command+["--apply","--staging-dir",str(self.project/"tmp")],capture_output=True,text=True)
        self.assertEqual(first.returncode,0,first.stdout+first.stderr)
        original=(target/"server.py").read_bytes()
        current=subprocess.run(command+["--apply"],capture_output=True,text=True)
        self.assertEqual(json.loads(current.stdout)["status"],"current")
        (target/"server.py").write_bytes(original+b"\n# local customization\n")
        conflict=subprocess.run(command+["--apply"],capture_output=True,text=True)
        self.assertEqual(conflict.returncode,2)
        self.assertIn("server.py",json.loads(conflict.stdout)["conflicts"])
        self.assertTrue((target/"server.py").read_bytes().endswith(b"# local customization\n"))


if __name__ == "__main__":unittest.main()
