import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
from redskill import archive, audit, config, inputs, network, workflow
from redskill.errors import DownloadError


def bundle(files=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as z:
        for name, value in (files or {"SKILL.md": "---\nname: different-internal-name\n---\n[ref](references/a.md)\n", "references/a.md": "`assets/missing.png`\n"}).items():
            info = zipfile.ZipInfo(name)
            info.filename = name  # Preserve malicious separators rather than Windows ZipInfo normalizing them.
            z.writestr(info, value)
    return stream.getvalue()


class FakeClient:
    def __init__(self, data=None, version="1.0.0"):
        self.data = data or bundle()
        self.version, self.downloads, self.manifests = version, 0, 0

    def manifest(self, identifier):
        self.manifests += 1
        return {"identifier": identifier, "version": self.version, "sha256": hashlib.sha256(self.data).hexdigest(),
                "zip_url": "https://sns-video-v3.xhscdn.com/test?secret=never-log"}

    def download(self, url, path):
        self.downloads += 1
        path.write_bytes(self.data)
        return len(self.data), hashlib.sha256(self.data).hexdigest()


class Cases(unittest.TestCase):
    def setUp(self):
        # Tests use an explicit workspace directory, never the operating system temp folder.
        parent = Path(os.environ.get("REDSKILL_TEST_ROOT", str(SKILL.parents[1] / "tmp/redskill-download/tests")))
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "workspace with spaces"
        self.work.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()

    def resolved(self, **kwargs):
        return config.resolve(self.work, cwd=self.work, home=self.home, environ={}, **kwargs)

    def test_share_text_and_distinct_identifiers(self):
        text = "现在为你安装Skill，先来检查是否已安装Red Skill商店，请根据 https://redskill.xiaohongshu.net/install.md 安装RedSkill 商店，然后安装qichelifestyle技能。若已安装，则直接安装 qichelifestyle 技能。"
        self.assertEqual(inputs.parse_identifier(text), "qichelifestyle")
        self.assertEqual(inputs.parse_identifier("redskill install alpha-1"), "alpha-1")
        with self.assertRaises(DownloadError) as error:
            inputs.parse_identifier("redskill install first; redskill install second")
        self.assertEqual(error.exception.code, "ambiguous_input")
        for value in ("../../x", "https://xhslink.com/test", "redskill install ../../x"):
            with self.assertRaises(DownloadError):
                inputs.parse_identifier(value)

    def test_config_precedence_empty_fallback_and_scope(self):
        (self.work / ".env").write_text("REDSKILL_DOWNLOAD_DIR=base\n", encoding="utf-8")
        (self.work / ".env.local").write_text("REDSKILL_DOWNLOAD_DIR=local\n", encoding="utf-8")
        (self.work / ".env.redskill-download").write_text("# comment\nREDSKILL_DOWNLOAD_DIR=dedicated\nREDSKILL_DOWNLOAD_DIR='last value'\n", encoding="utf-8")
        self.assertEqual(self.resolved()["output_directory"], str(self.work / "last value"))
        result = config.resolve(self.work, cwd=self.work, home=self.home, environ={config.KEY: "process"})
        self.assertEqual(result["source"], "process environment")
        (self.work / ".env.redskill-download").write_text("REDSKILL_DOWNLOAD_DIR= \n", encoding="utf-8")
        self.assertEqual(self.resolved()["configured_value"], "local")
        self.assertEqual(self.resolved(output="override")["source"], "command line")
        (self.work / ".env.another-skill").write_text("REDSKILL_DOWNLOAD_DIR=wrong\n", encoding="utf-8")
        self.assertEqual(self.resolved()["configured_value"], "local")

    def test_config_does_not_search_parents_and_relative_path_uses_workspace(self):
        (self.root / ".env").write_text("REDSKILL_DOWNLOAD_DIR=parent\n", encoding="utf-8")
        child = self.work / "nested"
        child.mkdir()
        (child / ".env.local").write_text('REDSKILL_DOWNLOAD_DIR="my downloads"\n', encoding="utf-8")
        result = config.resolve(self.work, cwd=child, home=self.home, environ={})
        self.assertEqual(result["output_directory"], str(self.work / "my downloads"))
        self.assertEqual(self.resolved()["configured_value"], config.DEFAULT)

    def test_global_is_explicit_and_save_preserves_other_settings(self):
        path = self.home / ".config/redskill-download/.env"
        path.parent.mkdir(parents=True)
        path.write_text("AUTO_UPDATE_CHECK=0\nOTHER=value\n", encoding="utf-8")
        config.configure("saved path", scope="global", cwd=self.work, home=self.home)
        self.assertEqual(self.resolved()["configured_value"], config.DEFAULT)
        self.assertEqual(self.resolved(use_global=True)["output_directory"], str(self.work / "saved path"))
        self.assertIn("AUTO_UPDATE_CHECK=0", path.read_text())
        self.assertIn("OTHER=value", path.read_text())
        config.configure("project path", scope="project", cwd=self.work, home=self.home)
        self.assertEqual(self.resolved(use_global=True)["configured_value"], "project path")

    def test_dotenv_does_not_execute_shell_or_expand_variables(self):
        (self.work / ".env").write_text("REDSKILL_DOWNLOAD_DIR=$(echo owned)/${HOME}\n", encoding="utf-8")
        self.assertEqual(self.resolved()["configured_value"], "$(echo owned)/${HOME}")

    def test_bundle_original_bytes_reuse_local_edit_and_versions(self):
        client = FakeClient()
        resolved = self.resolved()
        first = workflow.run("example", resolved, client=client)
        first_dir = Path(first["directory"])
        self.assertEqual((first_dir / "bundle.zip").read_bytes(), client.data)
        self.assertEqual(first["status"], "downloaded_with_missing_references")
        self.assertEqual(first["missing_references"][0]["path"], "assets/missing.png")
        self.assertEqual(first["file_count"], 2)
        self.assertNotIn("secret=", (first_dir / "manifest.json").read_text())
        again = workflow.run("example", resolved, client=client)
        self.assertTrue(again["reused"])
        self.assertEqual(client.downloads, 1)
        (first_dir / "skill/SKILL.md").write_text("my edit", encoding="utf-8")
        edited = workflow.run("example", resolved, client=client)
        self.assertNotEqual(edited["directory"], first["directory"])
        self.assertEqual((first_dir / "skill/SKILL.md").read_text(), "my edit")
        client.version = "2.0.0"
        second = workflow.run("example", resolved, client=client)
        self.assertEqual(Path(second["directory"]).name, "2.0.0")
        self.assertTrue(first_dir.exists())

    def test_added_file_and_mutated_inventory_cannot_hide_edits(self):
        client = FakeClient()
        first = workflow.run("example", self.resolved(), client=client)
        path = Path(first["directory"])
        (path / "skill/new.txt").write_text("mine")
        (path / "inventory.json").write_text('{"files": []}')
        self.assertFalse(archive.unchanged(path, first["sha256"]))
        second = workflow.run("example", self.resolved(), client=client)
        self.assertNotEqual(second["directory"], first["directory"])

    def test_missing_report_restored_and_edited_records_preserved(self):
        client = FakeClient()
        first = workflow.run("example", self.resolved(), client=client)
        folder = Path(first["directory"])
        report_path = folder / "report.json"
        original_report = json.loads(report_path.read_text(encoding="utf-8"))
        report_path.unlink()
        again = workflow.run("example", self.resolved(), client=client)
        self.assertTrue(again["reused"])
        self.assertEqual(client.downloads, 1)
        self.assertEqual(json.loads(Path(again["report_path"]).read_text(encoding="utf-8")), original_report)
        report_path.write_text('{"my_note": "preserve"}', encoding="utf-8")
        edited = workflow.run("example", self.resolved(), client=client)
        self.assertFalse(edited["reused"])
        self.assertNotEqual(edited["directory"], first["directory"])
        self.assertEqual(json.loads(report_path.read_text()), {"my_note": "preserve"})
        self.assertTrue(Path(edited["report_path"]).is_file())

    def test_missing_manifest_or_inventory_gets_complete_new_copy(self):
        for record in ("manifest.json", "inventory.json"):
            with self.subTest(record=record):
                client = FakeClient()
                name = record.split(".")[0]
                first = workflow.run(name, self.resolved(), client=client)
                (Path(first["directory"]) / record).unlink()
                again = workflow.run(name, self.resolved(), client=client)
                self.assertFalse(again["reused"])
                self.assertNotEqual(again["directory"], first["directory"])
                for required in ("manifest.json", "inventory.json", "report.json", "bundle.zip"):
                    self.assertTrue((Path(again["directory"]) / required).is_file())

    def test_empty_directory_changes_are_preserved(self):
        client = FakeClient(bundle({"SKILL.md": "example", "empty/": ""}))
        first = workflow.run("example", self.resolved(), client=client)
        folder = Path(first["directory"])
        self.assertTrue(archive.unchanged(folder, first["sha256"]))
        (folder / "skill/empty").rmdir()
        self.assertFalse(archive.unchanged(folder, first["sha256"]))
        again = workflow.run("example", self.resolved(), client=client)
        self.assertFalse(again["reused"])
        self.assertTrue((Path(again["directory"]) / "skill/empty").is_dir())
        self.assertFalse((folder / "skill/empty").exists())

    def test_sha_mismatch_does_not_publish(self):
        client = FakeClient()
        original = client.manifest
        client.manifest = lambda name: {**original(name), "sha256": "0" * 64}
        with self.assertRaises(DownloadError) as error:
            workflow.run("example", self.resolved(), client=client)
        self.assertEqual(error.exception.code, "sha256_mismatch")
        self.assertFalse((Path(self.resolved()["output_directory"]) / "example/1.0.0").exists())

    def test_completed_zip_reused_after_interruption(self):
        client = FakeClient()
        with patch.object(audit, "audit", side_effect=DownloadError("interrupted", "synthetic interruption")):
            with self.assertRaises(DownloadError):
                workflow.run("example", self.resolved(), client=client)
        result = workflow.run("example", self.resolved(), client=client)
        self.assertEqual(client.downloads, 1)
        self.assertTrue(Path(result["zip_path"]).exists())

    def test_same_identifier_lock_excludes_second_writer(self):
        path = self.root / "test.lock"
        with workflow.lock(path):
            with self.assertRaises(DownloadError) as error:
                with workflow.lock(path):
                    pass
            self.assertEqual(error.exception.code, "task_busy")
        with workflow.lock(path):
            pass

    def test_saved_cooldown_checked_before_any_network(self):
        from datetime import datetime, timezone
        import time
        output = Path(self.resolved()["output_directory"])
        state = output / ".redskill-state/example.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"retry_at": datetime.fromtimestamp(time.time() + 120, timezone.utc).isoformat()}))
        client = FakeClient()
        with self.assertRaises(DownloadError) as error:
            workflow.run("example", self.resolved(), client=client)
        self.assertEqual(error.exception.code, "retry_later")
        self.assertEqual(client.manifests, 0)

    def test_unsafe_zip_and_platform_collisions(self):
        for files in ({"../escape": "x"}, {"/absolute": "x"}, {"x\\bad": "x"}, {"NUL.txt": "x"},
                      {"a.txt": "1", "A.txt": "2"}, {"file": "x", "file/child": "x"}):
            path = self.root / "unsafe.zip"
            path.write_bytes(bundle(files))
            with self.subTest(paths=list(files)), self.assertRaises(DownloadError):
                archive.inspect_zip(path)
        path = self.root / "link.zip"
        with zipfile.ZipFile(path, "w") as z:
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "target")
        with self.assertRaises(DownloadError):
            archive.inspect_zip(path)

    def test_crc_failure_and_size_limit(self):
        path = self.root / "corrupt.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
            z.writestr("SKILL.md", b"UNIQUE_CONTENT")
        data = path.read_bytes().replace(b"UNIQUE_CONTENT", b"BROKEN_CONTENT")
        path.write_bytes(data)
        with self.assertRaises(zipfile.BadZipFile):
            archive.extract(path, self.root / "expanded")
        path.write_bytes(bundle())
        with patch.object(archive, "MAX_MEMBER", 1), self.assertRaises(DownloadError):
            archive.inspect_zip(path)

    def test_audit_nested_skill_example_and_external_link(self):
        tree = self.root / "tree"
        (tree / "wrapper/references").mkdir(parents=True)
        (tree / "wrapper/SKILL.md").write_text('[ok](references/a.md)\n[external](https://example.com/missing.png)\n```\n`assets/output.png`\n```\n', encoding="utf-8")
        (tree / "wrapper/references/a.md").write_text('`assets/missing.png`\n`assets/{name}.png`\n', encoding="utf-8")
        files = [{"path": p.relative_to(tree).as_posix()} for p in tree.rglob("*") if p.is_file()]
        report = audit.audit(tree, files)
        self.assertEqual([r["path"] for r in report["missing_references"]], ["wrapper/assets/missing.png"])
        self.assertEqual(len(report["review_hints"]), 2)


class NetworkCases(unittest.TestCase):
    def test_url_scope_and_http_upgrade(self):
        self.assertEqual(network.secure_cdn_url("http://sns-video-v6.xhscdn.com/a?signature=x"), "https://sns-video-v6.xhscdn.com/a?signature=x")
        for url in ("http://evil.test/a", "https://xhscdn.com.evil.test/a", "https://user:pass@sns-video-v6.xhscdn.com/a", "https://127.0.0.1/a", "https://sns-video-v6.xhscdn.com:80/a"):
            with self.assertRaises(DownloadError):
                network.secure_cdn_url(url)

    def test_retry_after_date_and_seconds(self):
        self.assertEqual(network.retry_delay("90", now=0), 90)
        self.assertEqual(network.retry_delay("Thu, 01 Jan 1970 00:02:00 GMT", now=0), 120)
        self.assertIsNone(network.retry_delay("invalid"))

    def test_long_cooldown_is_not_truncated(self):
        class Opener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(request.full_url, 429, "rate limited", {"Retry-After": "120"}, None)
        sleeps = []
        client = network.Client(opener=Opener(), sleep=sleeps.append)
        with self.assertRaises(DownloadError) as error:
            client.manifest("example")
        self.assertEqual(error.exception.code, "retry_later")
        self.assertIn("retry_at", error.exception.details)
        self.assertEqual(sleeps, [])

    def test_permission_failure_not_retried_or_leaked(self):
        class Opener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                raise urllib.error.HTTPError(request.full_url, 403, "denied secret", {}, None)
        opener = Opener()
        with self.assertRaises(DownloadError) as error:
            network.Client(opener=opener).manifest("example")
        self.assertEqual(error.exception.code, "access_denied")
        self.assertEqual(opener.calls, 1)
        self.assertNotIn("secret", str(error.exception))

    def test_transient_failure_is_bounded_by_attempts_and_budget(self):
        class Opener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                raise urllib.error.URLError("connection reset")
        elapsed = [0]
        opener = Opener()
        def sleep(seconds):
            elapsed[0] += seconds
        with self.assertRaises(DownloadError):
            network.Client(opener=opener, clock=lambda: elapsed[0], sleep=sleep).manifest("example")
        self.assertEqual(opener.calls, 3)
        self.assertEqual(elapsed[0], 3)

    def test_host_permission_denial_is_not_retried(self):
        class Opener:
            calls = 0

            def open(self, request, timeout):
                self.calls += 1
                raise urllib.error.URLError(PermissionError("sandbox denied"))
        opener = Opener()
        sleeps = []
        with self.assertRaises(DownloadError) as error:
            network.Client(opener=opener, sleep=sleeps.append).manifest("example")
        self.assertEqual(error.exception.code, "permission_denied")
        self.assertEqual(opener.calls, 1)
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
