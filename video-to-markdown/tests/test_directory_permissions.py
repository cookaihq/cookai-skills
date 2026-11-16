"""Directory defaults survive task initialization, later writes and recovery."""
import os
from pathlib import Path
import stat

from video_to_markdown import paths, state
from skill_ui.storage import private_directory


def directory_permissions(path: Path):
    if os.name == "nt":
        import win32security

        information = win32security.DACL_SECURITY_INFORMATION
        descriptor = win32security.GetFileSecurity(str(path), information)
        return win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
            descriptor, win32security.SDDL_REVISION_1, information,
        )
    return stat.S_IMODE(path.stat().st_mode)


def test_task_directories_retain_parent_defaults_across_state_writes_and_load(tmp_path):
    reference = tmp_path / "default-directories"
    reference.mkdir()
    task = paths.create_task_directory(tmp_path, "20261116200222-source.video")
    relative_directories = (".", "assets", "assets/frames", "assets/motion", "source", ".state", ".state/batches")
    for relative in relative_directories:
        (reference / relative).mkdir(parents=True, exist_ok=True)

    manifest, private = state.initialize_task(task, settings_snapshot={"asr_provider": "aihub-audio"})
    state.save_manifest(task, manifest)
    state.save_private(task, private)
    state.atomic_write_json(task / ".state/batches/batch-0001.json", {"readback": True})
    loaded_task, loaded_manifest, loaded_private = state.load_task(task)
    assert loaded_task == task.resolve()
    assert loaded_manifest["task_id"] == loaded_private["task_id"] == manifest["task_id"]
    assert state.read_json(task / ".state/batches/batch-0001.json") == {"readback": True}

    for relative in relative_directories:
        directory = task / relative
        assert directory_permissions(directory) == directory_permissions(reference / relative), relative
        proof = directory / "read-write.txt"
        proof.write_text("inherited defaults", encoding="utf-8")
        assert proof.read_text(encoding="utf-8") == "inherited defaults"


def test_configuration_runtime_directories_retain_defaults_on_create_and_reuse(tmp_path):
    reference = tmp_path / "default-runtime"
    reference.mkdir()
    runtime = private_directory(tmp_path / "runtime")
    before = directory_permissions(runtime)
    assert before == directory_permissions(reference)
    private_directory(runtime)
    assert directory_permissions(runtime) == before
    nested = private_directory(runtime / "nested")
    proof = nested / "read-write.txt"
    proof.write_text("runtime readback", encoding="utf-8")
    assert proof.read_text(encoding="utf-8") == "runtime readback"
