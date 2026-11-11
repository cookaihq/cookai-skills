"""Relate delivered lines to source regions, including edits made by cleaning."""
from difflib import SequenceMatcher
import re


def raw_line_sources(raw: str, pages: list[dict]) -> list[list[dict]]:
    sources: list[list[dict]] = [[] for _ in raw.splitlines()]
    for page in pages:
        for block in page["blocks"]:
            start = page["document_line_start"] + block["line_start"] - 2
            end = page["document_line_start"] + block["line_end"] - 1
            for line in range(start, end):
                sources[line].append({"page": page["physical_page"], "block": block["id"]})
    return sources


def grouped(sources: list[list[dict]]) -> list[dict]:
    regions: list[dict] = []
    for number, source in enumerate(sources, 1):
        if not source:
            continue  # Empty separators and explicit missing-page notices have no source region.
        if regions and regions[-1]["line_end"] == number - 1 and regions[-1]["sources"] == source:
            regions[-1]["line_end"] = number
        else:
            regions.append({"line_start": number, "line_end": number, "sources": source})
    return regions


def cleaned_sources(raw: str, cleaned: str, sources: list[list[dict]], changes: list[dict]) -> list[dict]:
    before, after = raw.splitlines(), cleaned.splitlines()
    mapped: list[list[dict]] = [[] for _ in after]
    declared = {(change["page"], change["block"]) for change in changes}
    explicit: dict[int, list[dict]] = {}
    for change in changes:
        span = change.get("output_lines")
        if span is None:
            continue
        if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                or not 1 <= span[0] <= span[1] <= len(after)):
            raise ValueError("output_lines must be a valid inclusive cleaned output line range")
        for number in range(span[0] - 1, span[1]):
            explicit.setdefault(number, []).append({"page": change["page"], "block": change["block"]})
    changed_text = set()
    for operation, a, b, c, d in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if operation == "equal":
            mapped[c:d] = sources[a:b]
            continue
        affected = {(source["page"], source["block"]) for row in sources[a:b] for source in row}
        changed_text.update(before[a:b])
        changed_text.update(after[c:d])
        if c == d or not any(line.strip() for line in after[c:d]):
            continue
        # An insertion has no old span. The host can bind it to declared changes
        # with output_lines; a single declared source is already unambiguous.
        if not affected:
            bound = {(source["page"], source["block"]) for number in range(c, d)
                     for source in explicit.get(number, [])}
            affected = bound or (declared if len(declared) == 1 else set())
            if not affected:
                raise ValueError("Cleaning insertion has ambiguous provenance; give output_lines [start, end] in its source change")
        related = [{"page": page, "block": block} for page, block in sorted(affected)]
        mapped[c:d] = [related for _ in range(c, d)]
    for number, related in explicit.items():
        mapped[number] = related
    for number, text in enumerate(after):
        if text in changed_text and text.strip() and not re.fullmatch(r"[\s|:\-]+", text):
            candidates = {(source["page"], source["block"]) for index, old in enumerate(before)
                          if old == text for source in sources[index]}
            candidates.update((source["page"], source["block"]) for index, value in enumerate(after)
                              if value == text for source in mapped[index])
            if len(candidates) > 1 and number not in explicit:
                raise ValueError("Repeated cleaned text has ambiguous source; provide output_lines for its retained region")

    def by_source(lines, associations):
        result: dict[tuple[int, str], list[str]] = {}
        for text, related in zip(lines, associations):
            if text.strip():
                for source in related:
                    result.setdefault((source["page"], source["block"]), []).append(text)
        return result

    original, delivered = by_source(before, sources), by_source(after, mapped)
    changed = {key for key in original.keys() | delivered.keys() if original.get(key) != delivered.get(key)}
    if not changed <= declared:
        raise ValueError("Cleaning edits need changes for every affected source region: " + str(sorted(changed - declared)))
    return grouped(mapped)
