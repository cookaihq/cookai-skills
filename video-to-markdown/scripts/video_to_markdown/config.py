from __future__ import annotations

import os
import re
import json
from dataclasses import dataclass
from pathlib import Path

PROVIDERS = ("aihub-audio", "feishu-minutes")
PROVIDER_KEY = "VIDEO_TO_MARKDOWN_ASR_PROVIDER"
VISION_KEY = "VIDEO_TO_MARKDOWN_VISION"
IDENTITY_KEYS = tuple("VIDEO_TO_MARKDOWN_FEISHU_" + suffix for suffix in ("PROFILE", "APP_ID", "USER_OPEN_ID"))
KNOWN_NAMES = (
    PROVIDER_KEY, "VIDEO_TO_MARKDOWN_TASK_OUTPUT_DIR", "VIDEO_TO_MARKDOWN_SOURCE_VIDEO_DIR",
    "FFMPEG_BIN", "FFPROBE_BIN", "VIDEO_TO_MARKDOWN_ASR_MODEL", VISION_KEY, *IDENTITY_KEYS,
)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedConfig:
    asr_provider: str
    output_base: str | None
    source_video_dir: str | None
    ffmpeg_bin: str
    ffprobe_bin: str
    sources: dict[str, str]
    use_home: bool
    home_file: Path
    feishu_identity: dict[str, str]
    vision: dict | None = None

    def public_snapshot(self) -> dict:
        return {
            "asr_provider": self.asr_provider, "output_base": self.output_base,
            "source_video_dir": self.source_video_dir, "ffmpeg_bin": self.ffmpeg_bin,
            "ffprobe_bin": self.ffprobe_bin, "sources": dict(self.sources),
            "use_home": self.use_home,
            "vision": public_vision(self.vision),
        }


def parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key not in KNOWN_NAMES:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _read_dotenv(path: Path) -> dict[str, str]:
    try:
        return parse_dotenv(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise ConfigError("cannot read configuration: %s" % path) from exc


def public_vision(value: dict | None) -> dict | None:
    if value is None:
        return None
    return {**value, "routes": {host: {k: v for k, v in route.items() if k != "api_key"}
                                 for host, route in value.get("routes", {}).items()}}


def vision_group(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) - {"ask_each_task", "routes", "call_limit"}:
            raise ValueError()
        if type(value.get("ask_each_task", True)) is not bool or not isinstance(value.get("routes", {}), dict):
            raise ValueError()
        cap = value.get("call_limit")
        if cap is not None and (type(cap) is not int or cap < 1):
            raise ValueError()
        from .vision import validate_route
        for host, route in value.get("routes", {}).items():
            if host not in {"codex", "claude-code", "workbuddy", "any"}:
                raise ValueError()
            validate_route(route)
            if host == "any" and route["method"] == "host-agent":
                raise ValueError()
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise ConfigError(VISION_KEY + " must contain a valid atomic vision configuration (no values printed)") from exc


def resolve(environ: dict[str, str] | None = None, cwd: str | os.PathLike[str] | None = None,
            *, use_home: bool = False, home_config_dir: str | os.PathLike[str] | None = None,
            overrides: dict[str, dict[str, str]] | None = None, read_vision: bool = True) -> ResolvedConfig:
    env = dict(os.environ if environ is None else environ)
    invocation = (Path.cwd() if cwd is None else Path(cwd)).resolve()
    home_file = (Path(home_config_dir) if home_config_dir is not None else Path.home() / ".config" / "video-to-markdown") / ".env"
    layers = [("process environment", env)]
    for name in (".env.video-to-markdown", ".env.local", ".env"):
        path = invocation / name
        layers.append((str(path), (overrides or {}).get(str(path), _read_dotenv(path))))
    if use_home:
        layers.append((str(home_file), (overrides or {}).get(str(home_file), _read_dotenv(home_file))))
    sources: dict[str, str] = {}

    def one(field: str, key: str) -> str | None:
        for source, values in layers:
            value = (values.get(key) or "").strip()
            if value:
                sources[field] = source
                return value
        return None

    provider = one("asr_provider", PROVIDER_KEY) or "aihub-audio"
    if provider not in PROVIDERS:
        raise ConfigError(PROVIDER_KEY + " must be aihub-audio or feishu-minutes")
    identity = {}
    for source, values in layers:
        if any((values.get(key) or "").strip() for key in IDENTITY_KEYS):
            identity = {key.rsplit("FEISHU_", 1)[1].lower(): (values.get(key) or "").strip() for key in IDENTITY_KEYS}
            sources["feishu_identity"] = source
            break
    if one("legacy_model", "VIDEO_TO_MARKDOWN_ASR_MODEL"):
        raise ConfigError("VIDEO_TO_MARKDOWN_ASR_MODEL is retired; explicitly migrate its model intent to AIhub Audio before starting (no credentials were moved)")
    return ResolvedConfig(
        provider, one("output_base", "VIDEO_TO_MARKDOWN_TASK_OUTPUT_DIR"),
        one("source_video_dir", "VIDEO_TO_MARKDOWN_SOURCE_VIDEO_DIR"),
        one("ffmpeg_bin", "FFMPEG_BIN") or "ffmpeg", one("ffprobe_bin", "FFPROBE_BIN") or "ffprobe",
        sources, use_home, home_file, identity, vision_group(one("vision", VISION_KEY)) if read_vision else None,
    )


def save_default(resolved: ResolvedConfig, *, cwd: Path, provider: str) -> dict:
    source = resolved.sources.get("asr_provider")
    if source == "process environment":
        return {"effective": False, "reason": "process_environment_override"}
    if source is None and not resolved.use_home:
        return {"effective": False, "reason": "global_reading_not_enabled", "path": str(resolved.home_file)}
    target = Path(source) if source else resolved.home_file
    try:
        update_file(target, {PROVIDER_KEY: provider})
        after = resolve(cwd=cwd, use_home=resolved.use_home, home_config_dir=resolved.home_file.parent)
        effective = after.asr_provider == provider and after.sources.get("asr_provider") == str(target)
        return {"effective": effective, "path": str(target), "reason": "saved" if effective else "readback_not_effective"}
    except (OSError, ConfigError):
        return {"effective": False, "path": str(target), "reason": "save_or_readback_failed"}


def render_dotenv(text: str, changes: dict[str, str | None]) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    for key, value in changes.items():
        if key not in KNOWN_NAMES or (value is not None and any(char in value for char in "\r\n\0")):
            raise ConfigError("configuration value cannot be represented safely")
        pattern = re.compile(r"^[ \t]*" + re.escape(key) + r"[ \t]*=[^\r\n]*(?:\r?\n|$)", re.M)
        replacement = "" if value is None else key + "=" + value + newline
        if pattern.search(text):
            text = pattern.sub(lambda _: replacement, text)
        elif value is not None:
            text += (newline if text and not text.endswith("\n") else "") + replacement
    return text


def update_file(path: Path, changes: dict[str, str | None]) -> None:
    from . import state
    if path.is_symlink():
        raise ConfigError("refusing to replace a symlink configuration")
    existed = path.exists()
    original = path.read_bytes() if existed else b""
    text = original.decode("utf-8-sig")
    text = render_dotenv(text, changes)
    if (path.exists() != existed) or (existed and path.read_bytes() != original):
        raise ConfigError("configuration changed before save; retry from the current source")
    state.atomic_write_bytes(path, text.encode("utf-8"))
