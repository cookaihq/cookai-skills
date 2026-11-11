"""Recommendations are candidates only; launch always uses saved configuration."""
from copy import deepcopy

DEFAULTS = {
    "codex": {"model": "gpt-6.1-sol", "effort": "medium"},
    "claude": {"model": "claude-sonnet-5-5", "effort": "medium"},
}


def common_settings():
    return {"defaults": deepcopy(DEFAULTS), "presets": {
        "codex": {"name": "Codex 日常", "agent": "codex", **DEFAULTS["codex"]},
        "codex-deep": {"name": "Codex 深入分析", "agent": "codex", "model": "gpt-6-astra", "effort": "xhigh"},
        "claude": {"name": "Claude 日常", "agent": "claude", **DEFAULTS["claude"]},
        "claude-deep": {"name": "Claude 深入分析", "agent": "claude", "model": "claude-opus-5-5", "effort": "xhigh"},
    }}
