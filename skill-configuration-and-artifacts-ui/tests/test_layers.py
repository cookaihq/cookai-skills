import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "assets/runtime"))
sys.path.insert(0, str(ROOT / "examples"))
from layers_adapter import LayeredDemo
from skill_ui import UIError
from skill_ui.contracts import public_config

TMP = Path(os.environ.get("SKILL_UI_TEST_TMP", Path.cwd() / "tmp/skill-ui-tests")).resolve()
TMP.mkdir(parents=True, exist_ok=True)


class LayerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TMP)
        self.store = LayeredDemo(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def field(self, name, snapshot=None):
        return next(f for f in (snapshot or self.store.snapshot())["fields"] if f["id"] == name)

    def preview(self, target, changes):
        return self.store.preview(changes, self.store.snapshot()["revision"], target)

    def write(self, layer, data):
        self.store.path(layer).write_text(json.dumps(data), encoding="utf-8")

    def test_project_key_only_and_clear_fallback(self):
        state = self.store.snapshot()
        self.assertEqual(len(state["layers"]), 10)
        self.assertEqual(self.field("api_key", state)["effective"]["source"], "project_local")
        for name in ("model", "endpoint", "timeout", "retries", "format"):
            self.assertEqual(self.field(name, state)["effective"]["source"], "plugin_env")
        for change in ({"op":"unset"}, {"op":"set", "value":""}):
            preview = self.preview("project_local", {"api_key":change})
            self.assertEqual(self.field("api_key", preview)["effective"]["source"], "plugin_env")
        self.assertIn("api_key", json.loads(self.store.path("project_local").read_text()))

    def test_same_scope_priority_environment_and_shadowed_write(self):
        self.write("project_skill", {"model":"detail-v2"})
        self.write("project_env", {"retries":0})
        self.assertEqual(self.field("model")["effective"]["source"], "project_skill")
        self.assertEqual(self.field("retries")["effective"]["value"], 0)
        preview = self.preview("plugin_env", {"model":{"op":"set", "value":"speed-v1"}})
        self.assertEqual(self.field("model", preview)["effective"]["value"], "detail-v2")
        self.store.environment = {"model":"balanced-v1"}
        self.assertEqual(self.field("model")["effective"]["source"], "process")
        with self.assertRaises(UIError): self.preview("process", {"model":{"op":"unset"}})
        with self.assertRaises(UIError): self.preview("../escape", {"model":{"op":"unset"}})

    def test_disabled_global_missing_defaults_and_unreadable(self):
        self.store.global_enabled = False
        self.assertEqual(self.field("model")["effective"]["source"], "default")
        self.assertEqual(self.field("endpoint")["effective"]["status"], "missing")
        self.store.path("plugin_local").write_text("invalid json")
        self.assertEqual(self.field("model")["effective"]["status"], "default")
        self.store.global_enabled = True
        self.assertEqual(self.field("model")["effective"]["status"], "error")
        with self.assertRaises(UIError): self.store.save({"model":{"op":"set","value":"detail-v2"}}, self.store.snapshot()["revision"], "project_local")

    def test_atomic_record_not_merged_and_failure_not_saved(self):
        self.write("plugin_skill_env", {"binding":{"identity":"global", "workspace":"global", "resource":"complete"}})
        self.write("project_env", {"binding":{"identity":"project", "workspace":"local"}})
        self.assertEqual(self.field("binding")["effective"]["status"], "invalid")
        self.assertNotIn("resource", self.field("binding")["effective"]["value"])
        before = self.store.path("project_local").read_bytes()
        with self.assertRaises(UIError): self.store.save({"format":{"op":"set","value":"jpeg"}}, self.store.snapshot()["revision"], "project_local")
        self.assertEqual(before, self.store.path("project_local").read_bytes())
        fixed = self.preview("project_env", {"binding":{"op":"unset"}})
        self.assertEqual(self.field("binding", fixed)["effective"]["source"], "plugin_skill_env")

    def test_dependencies_required_and_revision_covers_all_inputs(self):
        preview = self.preview("project_local", {"retain":{"op":"set","value":False}})
        self.assertFalse(self.field("days", preview)["active"])
        preview = self.preview("project_local", {"retain":{"op":"set","value":False},"days":{"op":"set","value":5}})
        self.assertTrue(preview["validation_errors"])
        original = self.store.snapshot()["revision"]
        self.write("project_skill", {"format":"webp"})
        with self.assertRaises(UIError): self.store.save({"retries":{"op":"set","value":3}}, original, "plugin_env")
        original = self.store.snapshot()["revision"]
        self.store.environment = {"retries":0}
        with self.assertRaises(UIError): self.store.preview({}, original, "project_local")

    def test_masking_every_layer_and_unknown_keys_preserved(self):
        self.write("project_local", {"api_key":"demo-secret-middle-9999", "unrelated":"retain"})
        data = public_config(self.store.snapshot())
        self.assertNotIn("secret-middle", json.dumps(data))
        self.assertNotIn("demo-global-key-0001", json.dumps(data))
        self.assertNotIn("default", self.field("api_key", data))
        self.store.save({"retries":{"op":"set","value":0}}, self.store.snapshot()["revision"], "project_local")
        self.assertEqual(json.loads(self.store.path("project_local").read_text())["unrelated"], "retain")
        with self.assertRaises(UIError): self.preview("project_skill", {"api_key":{"op":"set", "value":"demo-new"}})


if __name__ == "__main__":
    unittest.main()
