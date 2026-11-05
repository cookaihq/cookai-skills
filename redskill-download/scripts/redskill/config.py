import os
from pathlib import Path

from .errors import DownloadError

KEY = "REDSKILL_DOWNLOAD_DIR"
DEFAULT = "downloads/redskill"


def read_env(path):
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise DownloadError("config_unreadable", "Cannot read configuration", path=str(path)) from exc
    values = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = (piece.strip() for piece in stripped.split("=", 1))
        if key != KEY:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def resolve(workspace, *, cwd=None, home=None, environ=None, use_global=False, output=None):
    cwd = Path(cwd or Path.cwd()).resolve()
    home = Path(home or Path.home())
    workspace = Path(workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise DownloadError("invalid_workspace", "Workspace must be an existing directory", path=str(workspace))
    environment = os.environ if environ is None else environ
    sources = [("process environment", environment)]
    for name in (".env.redskill-download", ".env.local", ".env"):
        path = cwd / name
        sources.append((str(path), read_env(path)))
    global_path = home / ".config/redskill-download/.env"
    if use_global:
        sources.append((str(global_path), read_env(global_path)))
    value, source = DEFAULT, "built-in default"
    for name, values in sources:
        candidate = str(values.get(KEY) or "").strip()
        if candidate:
            value, source = candidate, name
            break
    if output is not None:
        if not str(output).strip():
            raise DownloadError("invalid_output", "Output directory cannot be empty")
        value, source = str(output), "command line"
    # Dotenv never performs shell expansion. Only a leading home shorthand is supported.
    folder = Path(value).expanduser()
    folder = (workspace / folder).resolve()
    return {"workspace": str(workspace), "config_directory": str(cwd), "output_directory": str(folder),
            "configured_value": value, "source": source, "global_enabled": use_global,
            "global_config_file": str(global_path)}


def configure(value, *, scope, cwd=None, home=None):
    cwd, home = Path(cwd or Path.cwd()), Path(home or Path.home())
    if not value.strip() or any(char in value for char in "\r\n\0"):
        raise DownloadError("invalid_config_value", "Download directory must be a nonempty single line")
    path = home / ".config/redskill-download/.env" if scope == "global" else cwd / ".env.local"
    if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
        raise DownloadError("config_link", "Refusing to replace linked configuration", path=str(path))
    try:
        original = path.read_bytes() if path.exists() else None
        text = (original or b"").decode("utf-8-sig")
        lines = text.splitlines()
        updated = []
        for line in lines:
            if "=" in line and line.split("=", 1)[0].strip() == KEY:
                continue
            updated.append(line)
        # Quotes are data inside an unquoted value; preserve spaces using a quote not in the value.
        quote = '"' if '"' not in value else "'" if "'" not in value else None
        if quote is None:
            raise DownloadError("invalid_config_value", "Directory containing both quote styles cannot be saved")
        updated.append(KEY + "=" + quote + value + quote)
        newline = "\r\n" if "\r\n" in text else "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        if (path.read_bytes() if path.exists() else None) != original:
            raise DownloadError("config_changed", "Configuration changed; retry from its current contents")
        # One configuration writer is expected; this does not claim cross-process compare-and-swap.
        with path.open("w", encoding="utf-8", newline="") as stream:
            stream.write(newline.join(updated) + newline)
    except (OSError, UnicodeError) as exc:
        raise DownloadError("config_write_failed", "Could not save configuration", path=str(path)) from exc
    return str(path)
