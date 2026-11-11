"""Approved AS page backed by the same Store and native catalog as launch."""
import hmac
import logging
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from skill_ui import server as core
from skill_ui.contracts import Adapter, UIError
from as_runtime.catalog import discover
from as_runtime.config import Store
from as_runtime.recommendations import common_settings


class Settings(Adapter):
    title = "Agent 会话启动器"

    def __init__(self, project):
        self.project = project
        self.store = Store(os.environ.get("AS_UI_CONFIG_FILE"))
        self.catalog = None

    def report(self, saved=None):
        if self.catalog is None:
            self.catalog = discover(self.project)
        if saved is None:
            initialization = self.store.initialization("inspect")
            value, revision = initialization["settings"], initialization["revision"]
        else:
            value, revision = saved
            initialization = {"show": False}
        return {"settings": value, "revision": revision, "catalog": self.catalog,
                "source": str(self.store.path), "synthetic": False,
                "initialization": {**initialization, "settings": common_settings()}}

    def config(self):
        _, revision = self.store.read()
        return {"revision": revision, "layers": [], "fields": []}

    def action(self, action, payload):
        if action == "initialize":
            self.store.initialization("add", payload.get("revision"))
            logging.info("as_initialization_added_to_draft")
            return {"settings": common_settings()}
        if action == "dismiss":
            self.store.initialization("dismiss")
            logging.info("as_initialization_dismissed")
            return {"handled": True}
        if action == "catalog":
            self.catalog = discover(self.project)
            return {"catalog": self.catalog}
        if action != "save":
            raise UIError("missing", "此操作不存在。", 404)
        self.catalog = discover(self.project)
        saved = self.store.save(payload.get("settings"), payload.get("revision"), self.catalog,
                                initializing=payload.get("initializing", False))
        if saved[0]["defaults"] or saved[0]["presets"]:
            self.store.initialization("dismiss")
        logging.info("as_configuration_saved")
        return {"settingsReport": self.report(saved)}


class SettingsHandler(core.Handler):
    def state(self):
        state = super().state()
        state["settingsReport"] = self.server.adapter.report()
        return state

    def post(self):
        path = urlsplit(self.path).path
        if not path.startswith("/api/as/"):
            return super().post()
        self.check_origin(post=True)
        self.authorized()
        self.server.lifetime()
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self.server.csrf):
            raise UIError("csrf", "页面校验失败，请重新打开配置页。", 403)
        with self.server.lock:
            self.send(200, self.server.adapter.action(path.rsplit("/", 1)[-1], self.body()))


def build(project):
    core.STATIC = Path(__file__).parent
    core.Handler = SettingsHandler
    return Settings(project)
