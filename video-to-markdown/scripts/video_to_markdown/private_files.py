"""Platform-native protection and durable replacement of task files."""

from functools import lru_cache
from contextlib import contextmanager
import os
from pathlib import Path
import stat
from typing import Iterator


@contextmanager
def _windows_errors(path: Path, operation: str) -> Iterator[None]:
    import pywintypes

    try:
        yield
    except pywintypes.error as exc:
        raise OSError("cannot %s: %s (Windows error %s)" % (operation, path, exc.winerror)) from exc


@lru_cache(maxsize=1)
def _windows_identity():
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    return user


@lru_cache(maxsize=1)
def _windows_logon_sid():
    """The owner's logon group also passes a restricted token's second ACL check."""
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        groups = win32security.GetTokenInformation(token, win32security.TokenGroups)
    finally:
        token.Close()
    sessions = [sid for sid, flags in groups if flags & 0xC0000000 == 0xC0000000]  # SE_GROUP_LOGON_ID
    if len(sessions) > 1:
        raise ValueError("private state requires an unambiguous Windows logon session")
    return sessions[0] if sessions else None


def protect(path: Path, mode: int, *, assign_owner: bool = True) -> None:
    if path.is_dir():
        raise ValueError("private-file protection requires a file; directories retain inherited defaults: %s" % path)
    if os.name != "nt":
        os.chmod(path, mode)
        return
    with _windows_errors(path, "protect private state"):
        _protect_windows(path, assign_owner=assign_owner)


def _protect_windows(path: Path, *, assign_owner: bool = True) -> None:
    import win32security

    user, acl = _private_acl_windows()
    win32security.SetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
        | (win32security.OWNER_SECURITY_INFORMATION if assign_owner else 0)
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION, user if assign_owner else None, None, acl, None,
    )


def _private_acl_windows():
    import ntsecuritycon
    import win32security

    user = _windows_identity()
    acl = win32security.ACL()
    for identity in (user, win32security.ConvertStringSidToSid("S-1-5-18"),
                     win32security.ConvertStringSidToSid("S-1-5-32-544")):
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, 0, ntsecuritycon.FILE_ALL_ACCESS, identity)
    logon = _windows_logon_sid()
    if logon is not None:
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, 0, ntsecuritycon.FILE_ALL_ACCESS, logon)
    return user, acl


def create_private_file(path: Path) -> int:
    """Create an exclusive Windows temporary file with its DACL before any data is written."""
    import msvcrt
    import pywintypes
    import win32con
    import win32file
    import win32security

    _, acl = _private_acl_windows()
    security = win32security.SECURITY_DESCRIPTOR()
    security.SetSecurityDescriptorDacl(True, acl, False)
    security.SetSecurityDescriptorControl(win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = security
    with _windows_errors(path, "create private state file"):
        handle = win32file.CreateFile(
            str(path), win32con.GENERIC_WRITE, win32con.FILE_SHARE_READ, attributes,
            win32con.CREATE_NEW, win32con.FILE_ATTRIBUTE_NORMAL, None,
        )
    try:
        # Some native sandbox backends ignore post-creation ACL changes. Verify
        # the creation-time policy before writing, without changing owner or directories.
        verify_private(path, 0o600)
        descriptor = msvcrt.open_osfhandle(int(handle), os.O_WRONLY | os.O_BINARY)
        handle.Detach()  # the CRT descriptor now owns this handle
        return descriptor
    finally:
        handle.Close()


def verify_private(path: Path, mode: int) -> None:
    if path.is_dir():
        raise ValueError("private-file verification requires a file; directory permissions follow inherited defaults: %s" % path)
    if os.name != "nt":
        if stat.S_IMODE(path.stat().st_mode) != mode:
            raise ValueError("private state requires mode %04o: %s" % (mode, path))
        return
    with _windows_errors(path, "inspect private state permissions"):
        _verify_windows(path)


def _verify_windows(path: Path) -> None:
    import win32security

    descriptor = win32security.GetFileSecurity(
        str(path), win32security.DACL_SECURITY_INFORMATION | win32security.OWNER_SECURITY_INFORMATION,
    )
    _validate_windows_descriptor(descriptor, path, previous_logons=True)


def _owner_logon_sid(sid: str, owner: str) -> bool:
    """Accept only this owner's session or an expired, no-longer-effective logon SID."""
    import pywintypes
    import win32security

    parts = sid.split("-")
    if len(parts) != 6 or parts[:4] != ["S", "1", "5", "5"]:
        return False
    high, low = int(parts[4]), int(parts[5])
    if not (0 <= high <= 0xFFFFFFFF and 0 <= low <= 0xFFFFFFFF):
        return False
    try:
        session = win32security.LsaGetLogonSessionData((high << 32) | low)
    except pywintypes.error as exc:
        return exc.winerror == 1312  # ERROR_NO_SUCH_LOGON_SESSION; other errors fail closed.
    user = session.get("Sid")
    return user is not None and win32security.ConvertSidToStringSid(user) == owner


def _validate_windows_descriptor(descriptor, path: Path, *, previous_logons: bool = False) -> set[str]:
    import win32security

    sid_string = win32security.ConvertSidToStringSid
    owner = sid_string(_windows_identity())
    if sid_string(descriptor.GetSecurityDescriptorOwner()) != owner:
        raise ValueError("private state belongs to a different Windows user: %s" % path)
    allowed = {owner, "S-1-5-18", "S-1-5-32-544"}
    logon = _windows_logon_sid()
    if logon is not None:
        allowed.add(sid_string(logon))
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None or acl.GetAceCount() == 0:
        raise ValueError("private state has no usable restricted Windows ACL: %s" % path)
    identities = set()
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        identity = sid_string(ace[-1])
        if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE or (
            identity not in allowed and not (previous_logons and _owner_logon_sid(identity, owner))
        ):
            raise ValueError("private state grants access beyond its Windows owner/system/admins: %s" % path)
        identities.add(identity)
    return identities


def ensure_private_access(path: Path, mode: int) -> None:
    """Verify privacy and renew a file's logon ACE when the caller has WRITE_DAC.

    Restricted tokens may retain read access to legacy files but cannot repair
    their ACLs. Leave those files protected; a later write still fails closed.
    """
    if os.name != "nt":
        verify_private(path, mode)
        return
    import pywintypes
    import win32con
    import win32file
    import win32security

    with _windows_errors(path, "renew private state access"):
        handle = win32file.CreateFile(
            str(path), win32con.READ_CONTROL | win32con.MAXIMUM_ALLOWED,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None, win32con.OPEN_EXISTING,
            win32file.FILE_FLAG_OPEN_REPARSE_POINT | win32con.FILE_FLAG_BACKUP_SEMANTICS, None,
        )
        try:
            information = win32file.GetFileInformationByHandle(handle)
            if information[0] & (win32con.FILE_ATTRIBUTE_DIRECTORY | win32con.FILE_ATTRIBUTE_REPARSE_POINT) or information[7] != 1:
                raise ValueError("private state path is not a regular single-link file: %s" % path)
            flags = win32security.DACL_SECURITY_INFORMATION | win32security.OWNER_SECURITY_INFORMATION
            descriptor = win32security.GetSecurityInfo(handle, win32security.SE_FILE_OBJECT, flags)
            identities = _validate_windows_descriptor(descriptor, path, previous_logons=True)
            _, acl = _private_acl_windows()
            expected = {win32security.ConvertSidToStringSid(acl.GetAce(index)[-1]) for index in range(acl.GetAceCount())}
            if identities != expected:
                try:
                    win32security.SetSecurityInfo(
                        handle, win32security.SE_FILE_OBJECT,
                        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                        None, None, acl, None,
                    )
                except pywintypes.error as exc:
                    if exc.winerror != 5:
                        raise
                    return  # No token escalation or directory/global permission change.
                _validate_windows_descriptor(win32security.GetSecurityInfo(handle, win32security.SE_FILE_OBJECT, flags), path, previous_logons=True)
        finally:
            handle.Close()


def replace_file(source: Path, target: Path) -> None:
    if os.name == "nt":
        import win32file

        with _windows_errors(target, "replace state file"):
            win32file.MoveFileEx(str(source), str(target), 0x1 | 0x8)  # REPLACE_EXISTING | WRITE_THROUGH
        return
    os.replace(source, target)
    descriptor = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
