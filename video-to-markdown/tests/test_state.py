import stat
import os
from pathlib import Path

from video_to_markdown import state


def test_task_state_is_atomic_and_private(tmp_path: Path):
    task = tmp_path / "task"
    task.mkdir()
    manifest, private = state.initialize_task(
        task,
        settings_snapshot={
            "asr_provider": "aihub-audio",
            "sources": {},
        },
    )
    assert manifest["task_id"] == private["task_id"]
    if os.name == "nt":
        import win32security

        descriptor = win32security.GetFileSecurity(
            str(task / ".state" / "private.json"), win32security.DACL_SECURITY_INFORMATION,
        )
        assert descriptor.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
        acl = descriptor.GetSecurityDescriptorDacl()
        assert acl is not None
        assert all(
            win32security.ConvertSidToStringSid(acl.GetAce(index)[-1]) != "S-1-1-0"
            for index in range(acl.GetAceCount())
        )
    else:
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
