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


def protect(path: Path, mode: int) -> None:
    if os.name != "nt":
        os.chmod(path, mode)
        return
    with _windows_errors(path, "protect private state"):
        _protect_windows(path)


def _protect_windows(path: Path) -> None:
    import win32con
    import win32security
    import ntsecuritycon

    user = _windows_identity()
    acl = win32security.ACL()
    flags = (win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE) if path.is_dir() else 0
    for identity in (user, win32security.ConvertStringSidToSid("S-1-5-18"),
                     win32security.ConvertStringSidToSid("S-1-5-32-544")):
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, flags, ntsecuritycon.FILE_ALL_ACCESS, identity)
    win32security.SetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
        | win32security.OWNER_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION, user, None, acl, None,
    )


def verify_private(path: Path, mode: int) -> None:
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
    if path.is_dir() and not descriptor.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED:
        raise ValueError("private state directory must not inherit Windows permissions: %s" % path)
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
