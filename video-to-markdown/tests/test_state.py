import stat
from pathlib import Path

from video_to_markdown import state


def test_task_state_is_atomic_and_private_file_is_0600(tmp_path: Path):
    task = tmp_path / "task"
    task.mkdir()
    manifest, private = state.initialize_task(
        task,
        settings_snapshot={
            "asr_model": "paraformer-v2",
            "sources": {},
        },
    )
    assert manifest["task_id"] == private["task_id"]
    assert stat.S_IMODE((task / ".state" / "private.json").stat().st_mode) == 0o600
    loaded_task, loaded_manifest, loaded_private = state.load_task(task)
    assert loaded_task == task.resolve()
    assert loaded_manifest["task_id"] == loaded_private["task_id"]


def test_sha256_file_reports_identity(tmp_path: Path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"abc")
    digest, size = state.sha256_file(source)
    assert digest == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert size == 3
