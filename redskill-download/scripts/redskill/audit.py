import posixpath
import re
import urllib.parse
from pathlib import Path


def audit(tree, members):
    tree = Path(tree)
    paths = {item["path"] for item in members}
    skills = sorted(path for path in paths if Path(path).name == "SKILL.md")
    missing, uncertain = {}, {}
    for relative in sorted(paths):
        if Path(relative).suffix.lower() not in (".md", ".markdown", ".html", ".htm"):
            continue
        path = tree / relative
        if path.stat().st_size > 2 * 1024 * 1024:
            uncertain[(relative, "large_document")] = {"source": relative, "reason": "document exceeds inspection size limit"}
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            uncertain[(relative, "encoding")] = {"source": relative, "reason": "document is not UTF-8"}
            continue
        ancestors = [p for p in skills if relative.startswith(posixpath.dirname(p) + "/") or not posixpath.dirname(p)]
        skill_root = posixpath.dirname(max(ancestors, key=lambda p: len(posixpath.dirname(p)))) if ancestors else ""
        # Fenced examples often name future output files. Retain them as review hints, not definite omissions.
        fenced = False
        for number, line in enumerate(text.splitlines(), 1):
            if re.match(r"\s*(```|~~~)", line):
                fenced = not fenced
                continue
            candidates = [(m.group(1).strip("<>"), "link") for m in re.finditer(r"!?\[[^\]]*\]\(([^\s)]+)(?:\s+[^)]*)?\)", line)]
            candidates += [(m.group(1), "link") for m in re.finditer(r"(?:src|href)=[\"']([^\"']+)[\"']", line, re.I)]
            candidates += [(m.group(1), "literal") for m in re.finditer(r"`((?:\./)?(?:assets|references|scripts)/[^`\r\n]+)`", line)]
            for raw, kind in candidates:
                parsed = urllib.parse.urlsplit(raw)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                value = urllib.parse.unquote(parsed.path)
                if value.startswith(("/", "~")) or any(char in value for char in "{}*$<>|") or "..." in value:
                    uncertain[(relative, raw)] = {"source": relative, "line": number, "reference": raw, "reason": "external or dynamic path"}
                    continue
                if kind == "literal":
                    candidate = posixpath.normpath(posixpath.join(skill_root, value))
                else:
                    candidate = posixpath.normpath(posixpath.join(posixpath.dirname(relative), value))
                if candidate in paths or any(p.startswith(candidate.rstrip("/") + "/") for p in paths):
                    continue
                if fenced or candidate.startswith("../"):
                    uncertain[(relative, raw)] = {"source": relative, "line": number, "reference": raw, "reason": "example or external path"}
                    continue
                missing.setdefault(candidate, {"path": candidate, "references": []})["references"].append({"source": relative, "line": number, "reference": raw})
    return {"skill_entrypoints": skills, "missing_references": list(missing.values()),
            "review_hints": list(uncertain.values()),
            "reference_check": "missing_references" if missing else "no_missing_references_detected",
            "scope": "Static Markdown links, HTML src/href and literal assets/references/scripts paths; does not prove runtime dependency completeness."}
