import re

from .errors import DownloadError

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def parse_identifier(text):
    text = text.strip()
    if IDENTIFIER.fullmatch(text):
        return text
    candidates = set()
    patterns = [r"\bredskill\s+install\s+[`\"']?([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?![A-Za-z0-9_/-])",
                r"安装\s*[`\"']?([A-Za-z0-9][A-Za-z0-9_-]{0,63})[`\"']?\s*技能",
                r"安装\s*[`\"']?([A-Za-z0-9][A-Za-z0-9_-]{0,63})[`\"']?\s+[Ss][Kk][Ii][Ll][Ll]\b"]
    for pattern in patterns:
        candidates.update(re.findall(pattern, text, flags=re.I))
    candidates = {item for item in candidates if item.lower() not in {"redskill", "skill"}}
    if len(candidates) != 1:
        raise DownloadError("ambiguous_input" if candidates else "identifier_missing",
                            "Provide one exact RedSkill identifier or a share instruction containing it",
                            candidates=sorted(candidates))
    return candidates.pop()


def safe_component(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,95}", value):
        raise DownloadError("invalid_manifest", "Unsafe " + label + " in the manifest")
    if value.endswith((".", " ")) or re.match(r"(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", value):
        raise DownloadError("invalid_manifest", "Non-portable " + label + " in the manifest")
    return value
