from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading


def test_direct_download_freezes_source_and_changed_source_starts_related_task(pdf, tmp_path, command):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/limit"):
                self.send_response(429)
                self.send_header("Retry-After", "600")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(pdf.read_bytes())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        root = f"http://127.0.0.1:{server.server_port}"
        start = command("start", root + "/manual.pdf?token=synthetic-secret", "--project", tmp_path,
                        "--pages", "1", "--owner", "host", "--vision", "available")
        task = Path(start["task_dir"])
        assert (task / "source/document.pdf").read_bytes() == pdf.read_bytes()
        assert "synthetic-secret" not in (task / "events.jsonl").read_text(encoding="utf-8")
        blocked = command("start", root + "/limit", "--project", tmp_path, "--owner", "host", "--vision", "available", expect=2)
        assert blocked["kind"] == "DownloadDeferred" and "retry_at" in blocked
        changed = tmp_path / "changed.pdf"
        changed.write_bytes(pdf.read_bytes() + b"\n% new source\n")
        related = command("start", changed, "--project", tmp_path, "--related-task", task,
                          "--owner", "host", "--vision", "available")
        assert related["task_id"] != start["task_id"]
        assert (task / "source/document.pdf").read_bytes() != changed.read_bytes()
        assert start["task_id"] in (Path(related["task_dir"]) / "task.json").read_text(encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_git_data_ignored_without_untracking_existing_files(pdf, tmp_path, command):
    project = tmp_path / "git-project"
    project.mkdir()
    subprocess.run(["git", "init", str(project)], check=True, capture_output=True)
    old = project / "data/pdf-to-markdown/kept.txt"
    old.parent.mkdir(parents=True)
    old.write_text("already tracked")
    subprocess.run(["git", "-C", str(project), "add", "data"], check=True)
    start = command("start", pdf, "--project", project, "--pages", "1", "--owner", "host", "--vision", "available")
    result = subprocess.run(["git", "-C", str(project), "check-ignore", start["task_dir"]], capture_output=True)
    assert result.returncode == 0
    tracked = subprocess.run(["git", "-C", str(project), "ls-files"], capture_output=True, text=True, check=True)
    assert "data/pdf-to-markdown/kept.txt" in tracked.stdout
