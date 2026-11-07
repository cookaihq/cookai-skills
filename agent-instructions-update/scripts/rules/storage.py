"""Initialize local history without resetting an existing host-protected ACL."""
from __future__ import annotations
import os
from pathlib import Path
import subprocess

from skill_ui.contracts import UIError
from skill_ui.storage import private_directory


def protected_windows_directory(path: Path) -> bool:
    """Read DACL protection; this requires no permission to change the ACL."""
    import ctypes
    from ctypes import wintypes

    security = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    pointer = ctypes.c_void_p
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
        pointer, pointer, ctypes.POINTER(pointer), pointer, ctypes.POINTER(pointer)]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.GetSecurityDescriptorControl.argtypes = [pointer, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    security.GetSecurityDescriptorControl.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    descriptor, dacl = pointer(), pointer()
    result = security.GetNamedSecurityInfoW(str(path), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if result:
        raise ctypes.WinError(result)
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(dacl.value and control.value & 0x1000)  # SE_DACL_PROTECTED
    finally:
        kernel.LocalFree(descriptor)


def prepare_history_directory(path: Path, owner_path: Path) -> None:
    try:
        if path.is_symlink() or path.is_junction():
            raise UIError('path', '维护记录目录不能是符号链接或 junction。')
        if os.name == 'nt' and path.is_dir() and protected_windows_directory(path):
            # The UI or host already initialized this directory. Codex may grant
            # Modify to its sandbox without granting WRITE_DAC; retain that ACL.
            return
        private_directory(path, owner_path=owner_path)
    except (OSError, subprocess.SubprocessError) as exc:
        code = getattr(exc, 'returncode', None) or getattr(exc, 'winerror', None) or type(exc).__name__
        raise UIError('history_permissions', f'无法初始化维护记录目录：{path}；权限阶段错误 {code}。请核对当前执行身份和目录访问权限。') from None
