from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ALLOWED_ASR_MODELS = frozenset(
    {
        "paraformer-v2",
        "paraformer-8k-v2",
        "scribe-v2",
        "cohere-transcribe",
    }
)
DEFAULT_ASR_MODEL = "paraformer-v2"
CANONICAL_KEY = "AIHUB_API_KEY"
LEGACY_KEY = "X_API_KEY"
KNOWN_NAMES = (
    CANONICAL_KEY,
    LEGACY_KEY,
    "VIDEO_TO_MARKDOWN_ASR_MODEL",
    "VIDEO_TO_MARKDOWN_TASK_OUTPUT_DIR",
    "VIDEO_TO_MARKDOWN_SOURCE_VIDEO_DIR",
    "FFMPEG_BIN",
    "FFPROBE_BIN",
)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedConfig:
    api_key: str | None
    api_key_name: str | None
    asr_model: str
    output_base: str | None
    source_video_dir: str | None
    ffmpeg_bin: str
    ffprobe_bin: str
    sources: dict[str, str]

    def public_snapshot(self) -> dict:
        return {
            "asr_model": self.asr_model,
            "output_base": self.output_base,
            "source_video_dir": self.source_video_dir,
            "ffmpeg_bin": self.ffmpeg_bin,
            "ffprobe_bin": self.ffprobe_bin,
            "sources": dict(self.sources),
            "api_key_name": self.api_key_name,
            "api_key_masked": mask_key(self.api_key) if self.api_key else None,
        }


def parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in KNOWN_NAMES:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _read_dotenv(path: Path) -> dict[str, str]:
    try:
        return parse_dotenv(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def _first_nonempty(
    names: tuple[str, ...], layers: list[tuple[str, dict[str, str]]]
) -> tuple[str | None, str | None, str | None]:
    for source, values in layers:
        for name in names:
            value = (values.get(name) or "").strip()
            if value:
                return value, source, name
    return None, None, None


def resolve(
    environ: dict[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    *,
    use_home: bool = False,
    home_config_dir: str | os.PathLike[str] | None = None,
) -> ResolvedConfig:
    env = dict(os.environ if environ is None else environ)
    invocation_dir = Path.cwd() if cwd is None else Path(cwd)
    config_dir = (
        Path(home_config_dir).expanduser()
        if home_config_dir is not None
        else Path.home() / ".config" / "video-to-markdown"
    )
    layers: list[tuple[str, dict[str, str]]] = [
        ("process environment", env),
        (str(invocation_dir / ".env.local"), _read_dotenv(invocation_dir / ".env.local")),
        (str(invocation_dir / ".env"), _read_dotenv(invocation_dir / ".env")),
    ]
    if use_home:
        layers.append((str(config_dir / ".env"), _read_dotenv(config_dir / ".env")))

    sources: dict[str, str] = {}

    def one(public_name: str, names: tuple[str, ...]) -> tuple[str | None, str | None]:
        value, source, actual_name = _first_nonempty(names, layers)
        if source is not None:
            sources[public_name] = source
        return value, actual_name

    api_key, api_key_name = one("api_key", (CANONICAL_KEY, LEGACY_KEY))
    asr_model, _ = one("asr_model", ("VIDEO_TO_MARKDOWN_ASR_MODEL",))
    output_base, _ = one("output_base", ("VIDEO_TO_MARKDOWN_TASK_OUTPUT_DIR",))
    source_video_dir, _ = one(
        "source_video_dir", ("VIDEO_TO_MARKDOWN_SOURCE_VIDEO_DIR",)
    )
    ffmpeg_bin, _ = one("ffmpeg_bin", ("FFMPEG_BIN",))
    ffprobe_bin, _ = one("ffprobe_bin", ("FFPROBE_BIN",))

    model = asr_model or DEFAULT_ASR_MODEL
    if model not in ALLOWED_ASR_MODELS:
        raise ConfigError(
            "VIDEO_TO_MARKDOWN_ASR_MODEL must be one of: %s"
            % ", ".join(sorted(ALLOWED_ASR_MODELS))
        )
    return ResolvedConfig(
        api_key=api_key,
        api_key_name=api_key_name,
        asr_model=model,
        output_base=output_base,
        source_video_dir=source_video_dir,
        ffmpeg_bin=ffmpeg_bin or "ffmpeg",
        ffprobe_bin=ffprobe_bin or "ffprobe",
        sources=sources,
    )


def mask_key(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]
