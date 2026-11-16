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
    sid_string = win32security.ConvertSidToStringSid
    owner = sid_string(_windows_identity())
    if sid_string(descriptor.GetSecurityDescriptorOwner()) != owner:
        raise ValueError("private state belongs to a different Windows user: %s" % path)
    allowed = {owner, "S-1-5-18", "S-1-5-32-544"}
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None or acl.GetAceCount() == 0:
        raise ValueError("private state has no usable restricted Windows ACL: %s" % path)
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE or sid_string(ace[-1]) not in allowed:
            raise ValueError("private state grants access beyond its Windows owner/system/admins: %s" % path)


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
