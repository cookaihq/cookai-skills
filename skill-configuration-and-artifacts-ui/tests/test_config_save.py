"""Private saves exercised against real, isolated Git repositories."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "assets" / "runtime"))
from skill_ui import UIError
from skill_ui.contracts import public_config
from skill_ui.storage import JsonConfig, atomic_json, prepare_private_write

TMP = Path(os.environ.get("SKILL_UI_TEST_TMP", Path.cwd() / "tmp" / "skill-ui-tests")).resolve()
TMP.mkdir(parents=True, exist_ok=True)


class ConfigSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=TMP)
        self.project = Path(self.tmp.name)
        self.git("-c", "init.templateDir=", "init", "-q")
        self.path = self.project / "private" / "settings.json"
        self.fields = [{"id": "key", "label": "API Key", "sensitive": True, "required": True}]
        self.store = JsonConfig(self.path, self.fields, owner_path=self.project)
        self.changes = {"key": {"op": "set", "value": "synthetic-middle-private-value"}}

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, expected=0):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        result = subprocess.run(["git", "-C", str(self.project), *args], env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stderr.decode(errors="replace"))
        return result.stdout

    def save(self):
        return self.store.save(self.changes, self.store.snapshot()["revision"], "project")

    def ignored(self, path):
        self.git("check-ignore", "--quiet", "--", str(path))

    def test_first_save_protects_target_lock_and_actual_temporary_before_replace(self):
        replace = os.replace
        observed = []
        def verify(source, target):
            self.ignored(source)
            self.ignored(target)
            self.ignored(self.path.with_suffix(".json.lock"))
            observed.append(source)
            replace(source, target)
        with patch("skill_ui.storage.os.replace", side_effect=verify):
            self.save()
        self.assertEqual(len(observed), 1)
        self.assertEqual(json.loads(self.path.read_text())["key"], self.changes["key"]["value"])
        self.assertEqual(self.git("ls-files", "-z"), b"")
        self.assertFalse(list(self.path.parent.glob("*.tmp")))
        rules = (self.path.parent / ".gitignore").read_bytes()
        self.save()
        self.assertEqual((self.path.parent / ".gitignore").read_bytes(), rules)
        self.git("check-ignore", "--quiet", "--", "private/public.json", expected=1)

    def test_preview_and_invalid_save_do_not_prepare_files(self):
        revision = self.store.snapshot()["revision"]
        self.store.preview(self.changes, revision, "project")
        for changes, rev, target in ((self.changes, revision, "other"), (self.changes, "stale", "project"),
                                     ({"key": {"op": "unset"}}, revision, "project")):
            with self.assertRaises(UIError):
                self.store.save(changes, rev, target)
        self.assertFalse(self.path.parent.exists())
        self.assertFalse((self.project / ".gitignore").exists())

    def test_nested_negation_special_names_and_existing_newlines(self):
        self.path = self.project / "子目录 [one] !#" / "key [a]!# .json"
        self.path.parent.mkdir()
        (self.project / ".gitignore").write_bytes(b"*.json\r\n")
        ignore = self.path.parent / ".gitignore"
        before = b"# preserve me\r\n!*.json\r\n!*.lock\r\n!*.tmp"
        ignore.write_bytes(before)
        self.store = JsonConfig(self.path, self.fields, owner_path=self.project)
        self.save()
        self.ignored(self.path)
        self.ignored(self.path.with_name(self.path.name + ".abc.tmp"))
        self.assertTrue(ignore.read_bytes().startswith(before + b"\r\n"))
        self.assertNotIn(b"\n", ignore.read_bytes().replace(b"\r\n", b""))
        self.git("check-ignore", "--quiet", "--", str(self.path.parent / "key a!# .json"), expected=1)
        first = ignore.read_bytes()
        self.save()
        self.assertEqual(ignore.read_bytes(), first)

    def test_later_negation_is_repaired_without_removing_user_rules(self):
        self.save()
        ignore = self.path.parent / ".gitignore"
        before = ignore.read_bytes() + b"!settings.json\n"
        ignore.write_bytes(before)
        self.save()
        self.assertTrue(ignore.read_bytes().startswith(before))
        self.ignored(self.path)

    def test_tracked_target_refused_even_when_forced_into_ignored_directory(self):
        self.path.parent.mkdir()
        self.path.write_text('{"key":"old"}')
        (self.project / ".gitignore").write_text("private/\n")
        self.git("add", "-f", "--", str(self.path))
        index = self.git("ls-files", "--stage", "-z")
        before = self.path.read_bytes()
        with self.assertRaises(UIError) as error:
            self.save()
        self.assertEqual(error.exception.code, "tracked")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.git("ls-files", "--stage", "-z"), index)
        self.assertFalse(self.path.with_suffix(".json.lock").exists())

    def test_permission_failure_can_retry_same_draft_and_revision(self):
        revision = self.store.snapshot()["revision"]
        with patch("skill_ui.storage._append_ignore", side_effect=PermissionError("synthetic")):
            with self.assertRaises(UIError) as error:
                self.store.save(self.changes, revision, "project")
        self.assertEqual(error.exception.code, "git")
        self.assertNotIn(self.changes["key"]["value"], error.exception.message)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.store.snapshot()["revision"], revision)
        self.store.save(self.changes, revision, "project")

    def test_replace_failure_keeps_previous_config_and_cleans_temporary(self):
        self.save()
        before = self.path.read_bytes()
        revision = self.store.snapshot()["revision"]
        with patch("skill_ui.storage.os.replace", side_effect=PermissionError("synthetic")):
            with self.assertRaises(PermissionError):
                self.store.save({"key": {"op": "set", "value": "replacement"}}, revision, "project")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))
        self.assertEqual(self.store.snapshot()["revision"], revision)

    def test_private_recovery_directory_and_tracked_children(self):
        directory = self.project / "recover [private]"
        journal = directory / "pending.json"
        prepare_private_write(journal, private_directory=directory, owner_path=self.project)
        self.ignored(journal)
        self.ignored(directory / "future.lock")
        atomic_json(journal, {"synthetic": True}, protect_git=True, owner_path=self.project)
        self.git("add", "-f", "--", str(journal))
        with self.assertRaises(UIError) as error:
            prepare_private_write(directory / "other.json", private_directory=directory, owner_path=self.project)
        self.assertEqual(error.exception.code, "tracked")

    def test_existing_protection_is_not_rewritten(self):
        before = b"# user rules\nprivate/\n"
        (self.project / ".gitignore").write_bytes(before)
        self.save()
        self.assertEqual((self.project / ".gitignore").read_bytes(), before)
        self.assertFalse((self.path.parent / ".gitignore").exists())

    def test_git_context_does_not_redirect_checks(self):
        with patch.dict(os.environ, {"GIT_DIR": str(self.project / "missing.git"), "GIT_INDEX_FILE": str(self.project / "wrong-index")}):
            self.save()
        self.ignored(self.path)
        self.assertFalse((self.project / "wrong-index").exists())

    def test_git_unavailable_in_repository_fails_before_writing(self):
        with patch("skill_ui.storage.subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaises(UIError) as error:
                self.save()
        self.assertEqual(error.exception.code, "git")
        self.assertFalse(self.path.exists())

    def test_actual_git_result_is_verified_after_preparation(self):
        with patch("skill_ui.storage._append_ignore"):
            with self.assertRaises(UIError) as error:
                self.save()
        self.assertEqual(error.exception.code, "ignore")
        self.assertFalse(self.path.exists())

    def test_links_and_invalid_paths_are_rejected(self):
        outside = self.project / "other"
        outside.mkdir()
        try:
            self.path.parent.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("OS cannot create a directory symlink")
        with self.assertRaises(UIError):
            self.save()
        self.assertFalse(list(outside.iterdir()))
        with self.assertRaises(UIError):
            prepare_private_write(self.project / ".gitignore")
        with self.assertRaises(UIError):
            prepare_private_write(self.project / "bad\nname")

    def test_real_key_mapping_survives_public_projection_without_secret(self):
        self.save()
        snapshot = self.store.snapshot()
        self.assertEqual(snapshot["fields"][0]["config_key"], '$["key"]')
        # A business adapter may map its UI identity to a completely different key.
        field = snapshot["fields"][0]
        field.update(id="credential_control", config_key="SERVICE_API_KEY")
        public = public_config(snapshot)
        self.assertEqual(public["fields"][0]["config_key"], "SERVICE_API_KEY")
        self.assertNotIn(self.changes["key"]["value"], json.dumps(public))
        self.assertNotIn("value", public["fields"][0]["effective"])
        self.assertTrue(all("value" not in row for row in public["fields"][0]["rows"]))


if __name__ == "__main__":
    unittest.main()
