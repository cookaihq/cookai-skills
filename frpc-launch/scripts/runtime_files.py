"""Filesystem protection shared by configuration and lifecycle commands."""
import os


def protect(path):
    if os.name == "nt":
        from windows_runtime import private_acl
        try:
            private_acl(path)
        except OSError as exc:
            raise OSError(exc.errno, "无法设置 Windows 私有 ACL；请使用配置所属用户或宿主正常授权流程。"
                          + str(exc.strerror), str(path), exc.winerror) from None
    else:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
