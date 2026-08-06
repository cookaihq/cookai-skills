import contextlib
import importlib.util
import io
import json
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


IMAGE_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "image_task.py"
IMAGE_SPEC = importlib.util.spec_from_file_location("image_2_handoff_runtime", IMAGE_SCRIPT)
image_runtime = importlib.util.module_from_spec(IMAGE_SPEC)
sys.modules[IMAGE_SPEC.name] = image_runtime
IMAGE_SPEC.loader.exec_module(image_runtime)

S3_SCRIPTS = Path(__file__).resolve().parents[3] / "s3-upload" / "scripts"
sys.path.insert(0, str(S3_SCRIPTS))
S3_SPEC = importlib.util.spec_from_file_location("s3_upload_handoff_runtime", S3_SCRIPTS / "upload.py")
s3_runtime = importlib.util.module_from_spec(S3_SPEC)
sys.modules[S3_SPEC.name] = s3_runtime
S3_SPEC.loader.exec_module(s3_runtime)

from s3 import Response


NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


class FakeImageApi:
    def __init__(self):
        self.requests = []
        self.responses = [
            image_runtime.HttpResponse(200, (), b'{"id":"task-handoff-1"}'),
            image_runtime.HttpResponse(
                200,
                (),
                b'{"status":"completed","results":[{"url":"https://images.example.test/cover.png","content_type":"image/png"}]}',
            ),
        ]

    def request(self, method, url, headers, body=None):
        self.requests.append((method, url, dict(headers), body))
        return self.responses.pop(0)


class FakeImageDownload:
    def __init__(self):
        self.requests = []

    def fetch(self, request, sink):
        self.requests.append(request)
        sink.write(b"generated-cover-bytes")
        return image_runtime.OneHopResponse(200, ())


def configure_s3_project(project: Path, *, public: bool) -> None:
    target_dir = project / ".s3-upload" / "targets"
    target_dir.mkdir(parents=True)
    access = (
        {
            "mode": "public",
            "public_base_url": "https://cdn.example.test/assets",
            "presign_expires_seconds": None,
        }
        if public
        else {
            "mode": "private",
            "public_base_url": None,
            "presign_expires_seconds": 3600,
        }
    )
    target = {
        "schema_version": 1,
        "credential": "project:image-key",
        "provider": "aws-s3",
        "region": "us-east-1",
        "endpoint": None,
        "addressing": None,
        "bucket": "project-artifacts",
        "prefix": "website-images/",
        "access": access,
        "retention": {"mode": "retain", "days": None},
        "collision": "replace",
        "object_headers": {
            "cache_control": None,
            "content_disposition": None,
        },
        "limits": {
            "soft_max_bytes": 104857600,
            "multipart_threshold_bytes": None,
            "part_size_bytes": None,
        },
        "retry": {"part_max_attempts": 3, "collision_max_attempts": 3},
        "setup": {
            "exclusive_prefix": public,
            "integration_test": False,
            "cors": None,
        },
    }
    (target_dir / "website-images.json").write_text(
        json.dumps(target), encoding="utf-8",
    )
    (project / ".s3-upload" / "config.json").write_text(
        json.dumps({
            "schema_version": 1,
            "default_target": None,
            "skill_targets": {"image-2": "project:website-images"},
        }),
        encoding="utf-8",
    )
    env_local = project / ".env.local"
    env_local.write_text(
        "S3_UPLOAD_PROJECT_CREDENTIALS_JSON="
        + json.dumps({
            "image-key": {
                "access_key_id": "PROJECTKEY1234",
                "secret_access_key": "project-secret-value",
                "session_token": "",
                "expires_at": None,
            }
        }, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    env_local.chmod(0o600)


def run_test_handoff(document, *, project: Path, transport, persistent_requested: bool):
    if not persistent_requested:
        return []
    results = []
    for output in document["outputs"]:
        if output["status"] != "saved" or output["local_path"] is None:
            continue
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = s3_runtime.main(
                [
                    "upload", "--file", output["local_path"],
                    "--caller-skill", "image-2", "--json",
                ],
                environ={},
                cwd=str(project),
                config_home=str(project / "home"),
                transport=transport,
                now=NOW,
            )
        if not stdout.getvalue():
            raise AssertionError(
                f"s3-upload emitted no JSON (exit={exit_code}): {stderr.getvalue()}"
            )
        results.append((exit_code, json.loads(stdout.getvalue()), stderr.getvalue()))
    return results


class ImageToS3HandoffTests(unittest.TestCase):
    def generate(self, project: Path, output: Path):
        api = FakeImageApi()
        download = FakeImageDownload()
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = image_runtime.main(
            [
                "--json", "--prompt", "cover", "--output-dir", str(output),
                "--filename", "cover", "--max-attempts", "1",
                "--poll-interval", "1",
            ],
            api_transport=api,
            download_transport=download,
            resolver=lambda host, port: ["8.8.8.8"],
            sleeper=lambda seconds: None,
            environ={"AIHUB_API_KEY": "test-handoff-image-key"},
            cwd=project,
            home=project / "home",
            stdout=stdout,
            stderr=stderr,
        )
        self.assertEqual(exit_code, 0, stderr.getvalue())
        return json.loads(stdout.getvalue()), api, download

    def test_mapping_does_not_upload_without_persistent_request(self):
        with tempfile.TemporaryDirectory() as project_text, tempfile.TemporaryDirectory() as output_text:
            project = Path(project_text).resolve()
            output = Path(output_text).resolve()
            configure_s3_project(project, public=False)
            document, _, _ = self.generate(project, output)
            s3_calls = []

            results = run_test_handoff(
                document,
                project=project,
                transport=lambda *args: s3_calls.append(args),
                persistent_requested=False,
            )

            self.assertEqual(results, [])
            self.assertEqual(s3_calls, [])

    def test_saved_external_output_uses_original_project_mapping_once(self):
        for public in (False, True):
            with self.subTest(public=public):
                with tempfile.TemporaryDirectory() as project_text, tempfile.TemporaryDirectory() as output_text:
                    project = Path(project_text).resolve()
                    output = Path(output_text).resolve()
                    self.assertNotEqual(project, output)
                    configure_s3_project(project, public=public)
                    document, api, download = self.generate(project, output)
                    self.assertEqual(
                        stat.S_IMODE((project / ".env.local").stat().st_mode),
                        0o600,
                    )
                    s3_calls = []

                    def s3_transport(method, url, headers, body):
                        s3_calls.append((method, url, dict(headers), body))
                        return Response(200, headers={"x-amz-version-id": "version-1"})

                    results = run_test_handoff(
                        document,
                        project=project,
                        transport=s3_transport,
                        persistent_requested=True,
                    )

                    self.assertEqual(len(api.requests), 2)
                    self.assertEqual(len(download.requests), 1)
                    self.assertEqual(len(results), 1)
                    exit_code, result, stderr = results[0]
                    self.assertEqual(exit_code, 0, stderr)
                    self.assertEqual(result["status"], "ok")
                    self.assertEqual(len(s3_calls), 1)
                    self.assertEqual(s3_calls[0][0], "PUT")
                    self.assertEqual(
                        s3_calls[0][1],
                        "https://project-artifacts.s3.amazonaws.com/website-images/cover.png",
                    )
                    self.assertEqual(s3_calls[0][3], b"generated-cover-bytes")
                    self.assertEqual(
                        result["object_reference"]["target_ref"],
                        "project:website-images",
                    )
                    if public:
                        self.assertEqual(result["url_kind"], "public")
                        self.assertEqual(
                            result["url"],
                            "https://cdn.example.test/assets/website-images/cover.png",
                        )
                    else:
                        self.assertEqual(result["url_kind"], "presigned")
                        self.assertIn("X-Amz-Signature=", result["url"])


if __name__ == "__main__":
    unittest.main()
