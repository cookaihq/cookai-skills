import stat
import os
from pathlib import Path

import pytest

from video_to_markdown import private_files, state


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


def test_failed_atomic_replace_preserves_primary_error_when_cleanup_also_fails(tmp_path: Path, monkeypatch):
    primary = OSError("original replacement failure")

    def fail_replace(source, target):
        raise primary

    def fail_cleanup(path):
        raise PermissionError("temporary deletion denied")

    monkeypatch.setattr(private_files, "replace_file", fail_replace)
    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    target = tmp_path / "manifest.json"
    with pytest.raises(OSError, match="original replacement failure") as error:
        state.atomic_write_json(target, {"task_id": "not-committed"})
    assert error.value is primary
    assert any("temporary deletion denied" in note for note in primary.__notes__)
    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows restricted-token access check")
def test_private_state_allows_restricted_owner_session_and_rejects_other_sessions(tmp_path: Path):
    import win32api
    import win32con
    import win32security

    task = tmp_path / "task"
    state.initialize_task(task, settings_snapshot={})
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY | win32con.TOKEN_DUPLICATE)
    try:
        groups = win32security.GetTokenInformation(token, win32security.TokenGroups)
        logon_sids = [sid for sid, flags in groups if flags & 0xC0000000 == 0xC0000000]
        assert len(logon_sids) == 1
        session_sids = [logon_sids[0], win32security.ConvertStringSidToSid("S-1-5-5-12345-67890")]
        for session, expected in zip(session_sids, (True, False)):
            restricted = win32security.CreateRestrictedToken(token, 0, None, None, [(session, 0)])
            impersonation = win32security.DuplicateToken(restricted, win32security.SecurityImpersonation)
            try:
                win32security.ImpersonateLoggedOnUser(impersonation)
                for relative in ("manifest.json", ".state/private.json"):
                    path = task / relative
                    if expected:
                        assert state.read_json(path)["schema_version"] == 4
                        with path.open("r+b") as handle:
                            assert handle.read(1) == b"{"
                            handle.seek(0)
                            handle.write(b"{")
                    else:
                        with pytest.raises(state.StateError):
                            state.read_json(path)
            finally:
                win32security.RevertToSelf()
                impersonation.Close()
                restricted.Close()
    finally:
        token.Close()


@pytest.mark.skipif(os.name != "nt", reason="Windows private-state ACL recovery")
def test_resume_legacy_private_files_keeps_contents_and_allows_restricted_owner(tmp_path: Path):
    import win32api
    import win32con
    import win32security

    task = tmp_path / "task"
    manifest, _ = state.initialize_task(task, settings_snapshot={})
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY | win32con.TOKEN_DUPLICATE)
    try:
        owner = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        owner_text = win32security.ConvertSidToStringSid(owner)
        descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            "D:P(A;;FA;;;%s)(A;;FA;;;SY)(A;;FA;;;BA)" % owner_text, 1,
        )
        before = {}
        for relative in ("manifest.json", ".state/private.json"):
            path = task / relative
            before[relative] = path.read_bytes()
            win32security.SetNamedSecurityInfo(str(path), 1, win32security.DACL_SECURITY_INFORMATION
                | win32security.PROTECTED_DACL_SECURITY_INFORMATION, None, None, descriptor.GetSecurityDescriptorDacl(), None)
        # Public recovery checks/renews only the owned private files. It must
        # remain usable when a later command runs with a restricted owner token.
        _, loaded, _ = state.load_task(task)
        assert loaded["task_id"] == manifest["task_id"]
        logon = next(sid for sid, flags in win32security.GetTokenInformation(token, win32security.TokenGroups)
                     if flags & 0xC0000000 == 0xC0000000)
        restricted = win32security.CreateRestrictedToken(token, 0, None, None, [(logon, 0)])
        impersonation = win32security.DuplicateToken(restricted, win32security.SecurityImpersonation)
        try:
            win32security.ImpersonateLoggedOnUser(impersonation)
            for relative, content in before.items():
                assert (task / relative).read_bytes() == content
        finally:
            win32security.RevertToSelf()
            impersonation.Close()
            restricted.Close()
    finally:
        token.Close()


@pytest.mark.skipif(os.name != "nt", reason="Windows private-state renewal boundaries")
def test_private_access_renewal_rejects_shared_or_linked_files_without_changing_them(tmp_path: Path):
    import win32security

    task = tmp_path / "task"
    state.initialize_task(task, settings_snapshot={})
    path = task / ".state/private.json"
    information = win32security.DACL_SECURITY_INFORMATION
    descriptor = win32security.GetFileSecurity(str(path), information)
    acl = descriptor.GetSecurityDescriptorDacl()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, 1, win32security.ConvertStringSidToSid("S-1-1-0"))
    win32security.SetNamedSecurityInfo(str(path), 1, information | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                                     None, None, acl, None)
    before = path.read_bytes()
    shared_acl = win32security.GetFileSecurity(str(path), information).GetSecurityDescriptorDacl()
    with pytest.raises(state.StateError, match="grants access beyond"):
        state.load_task(task)
    after_acl = win32security.GetFileSecurity(str(path), information).GetSecurityDescriptorDacl()
    assert [after_acl.GetAce(i) for i in range(after_acl.GetAceCount())] == [
        shared_acl.GetAce(i) for i in range(shared_acl.GetAceCount())]
    assert path.read_bytes() == before

    private_files.protect(path, 0o600)
    alias = tmp_path / "private-alias.json"
    alias.hardlink_to(path)
    with pytest.raises(state.StateError, match="regular single-link file"):
        state.load_task(task)
    assert path.read_bytes() == alias.read_bytes() == before
