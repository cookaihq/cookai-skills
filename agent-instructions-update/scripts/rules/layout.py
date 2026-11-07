"""Create a reviewable layout plan, retaining moved directories for recovery."""
from __future__ import annotations
from pathlib import Path
from typing import Any
from .indexing import references, reference_text


def layout_changes(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    from .maintenance import snapshot, RuleError
    root = Path(data['root']).absolute()
    main = Path(data['source_main']).absolute()
    source = Path(data['source_scenarios']).absolute() if data.get('source_scenarios') else None
    target_main = root / 'AGENTS.md'
    target_scenarios = root / 'scenarios-agent-instructions'
    if source and (root.is_relative_to(source) or source.is_relative_to(root)) and source != target_scenarios:
        raise RuleError('layout_overlap', '旧场景目录与新物理根不能互相嵌套。')
    original = snapshot(str(main))
    if not original['exists'] or original['directory']:
        raise RuleError('source', '布局变更需要明确的现有主规则。')
    files = [(main, target_main)]
    known = [str(main)]
    if source:
        record = snapshot(str(source))
        if not record['directory']:
            raise RuleError('source', '场景来源须为已存在目录。')
        known.append(str(source))
        for relative, detail in record['tree'].items():
            if 'link' in detail:
                raise RuleError('nested_link', '场景目录包含链接，请先明确这些入口的迁移目标。')
            if 'hash' in detail:
                files.append((source/relative, target_scenarios/relative))
    mapping = {str(old.resolve()): new for old,new in files}
    changes: list[dict[str, Any]] = []
    if not target_scenarios.exists():
        changes.append({'path': str(target_scenarios), 'op': 'mkdir'})
    for old, new in files:
        content = snapshot(str(old))['content']
        for ref in reversed(references(old, content, data.get('reference_bases'))):
            if ref['target'] is None:
                raise RuleError('reference_basis', '有入口的路径基准待核实，请提供 reference_bases 后再迁移。')
            target = mapping.get(ref['target'], Path(ref['target']))
            content = content[:ref['start']] + reference_text(ref, target, new) + content[ref['end']:]
        if new != old:
            existing = snapshot(str(new))
            if existing['exists'] and existing['content'] != content:
                raise RuleError('layout_conflict', '新物理位置已有不同正文，请先审阅合并：'+str(new))
            changes.append({'path': str(new), 'content': content})
    for entrance in data.get('entrances', []):
        path = Path(entrance).absolute()
        if path != target_main:
            changes.append({'path': str(path), 'op': 'link', 'target': str(target_main)})
    scenario_entrances = [*data.get('scenario_entrances', [])]
    if source and source != target_scenarios:
        scenario_entrances.insert(0, str(source))
    for entrance in dict.fromkeys(scenario_entrances):
        if Path(entrance).absolute() != target_scenarios:
            changes.append({'path': entrance, 'op': 'link', 'target': str(target_scenarios)})
    return changes, known
