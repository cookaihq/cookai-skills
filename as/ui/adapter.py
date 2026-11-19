"""Approved AS page backed by the same Store and native catalog as launch."""
import hmac
import hashlib
import json
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
        self.last_save = None

    def report(self, saved=None):
        if self.catalog is None:
            self.catalog = discover(self.project)
        if saved is None:
            initialization = self.store.initialization("inspect")
            value, revision = initialization["settings"], initialization["revision"]
        else:
            value, revision = saved
            initialization = {"show": False}
        try:
            self.store.read()
            readiness = {'ready': True}
        except UIError as error:
            readiness = {'ready': False, 'code': error.code,
                         'message': '现有配置尚不能用于启动会话，请修复后再调用：' + error.message}
        return {"settings": value, "revision": revision, "catalog": self.catalog,
                "readiness": readiness,
                "record_revisions": {kind + '.' + key: self.store.record_revision(record)
                    for kind in ('defaults', 'presets') for key, record in value[kind].items()},
                "missing_record_revision": self.store.record_revision(None),
                "last_save": self.last_save,
                "source": str(self.store.path), "synthetic": False,
                "initialization": {**initialization, "settings": common_settings()}}

    def config(self):
        _, revision = self.store.read(tolerant=True)
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
        request_id = payload.get('request_id')
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
            raise UIError('request', '保存请求标识无效。')
        signature = hashlib.sha256(json.dumps(payload.get('changes'), sort_keys=True).encode()).hexdigest()
        if request_id and self.last_save and self.last_save['request_id'] == request_id:
            if signature != self.last_save['signature']:
                raise UIError('conflict', '此保存标识已用于其他内容，请重新读取。', 409)
            return self._last_response
        self.catalog = discover(self.project)
        saved, results = self.store.save_changes(payload.get('changes'), self.catalog)
        self.last_save = {'request_id': request_id, 'signature': signature, 'field_results': results}
        self._last_response = {"settingsReport": self.report(saved), 'field_results': results}
        if saved[0]["defaults"] or saved[0]["presets"]:
            try:
                self.store.initialization("dismiss")
            except UIError:
                logging.warning('as_initialization_receipt_unavailable')
        logging.info("as_configuration_saved")
        return self._last_response


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
