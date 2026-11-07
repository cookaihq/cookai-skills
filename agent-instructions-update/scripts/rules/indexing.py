"""Index scoped rule entrances and explicit Markdown references, never infer loading."""
from __future__ import annotations
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit

MAIN_NAMES = {'AGENTS.md', 'AGENTS.override.md', 'CLAUDE.md', 'CODEBUDDY.md', '.hermes.md', 'HERMES.md'}
EXCLUDED = {'.git', '.venv', 'node_modules', '.worktrees', '__pycache__', 'tmp', 'dist', 'build'}
LINK = re.compile(r'\[([^\]\n]+)\]\(<?([^)>\n]+)>?\)')
CODE_PATH = re.compile(r'`([^`\n]+\.md(?:#[^`\n]*)?)`|(?<!\w)@([^\s]+\.md(?:#[^\s]*)?)')


def references(path: Path, content: str, bases: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    from .maintenance import RuleError
    path = path.resolve()
    result = []
    matches = [(match[2], match.start(2), match.end(2), match[1], 'markdown') for match in LINK.finditer(content)]
    for match in CODE_PATH.finditer(content):
        group = 1 if match[1] is not None else 2
        if not any(start <= match.start() < end for _,start,end,_,_ in matches):
            matches.append((match[group], match.start(group), match.end(group), match[group], 'path_reference'))
    for raw, start, end, label, kind in sorted(matches, key=lambda item: item[1]):
        parsed = urlsplit(raw)
        if (parsed.scheme and not re.match(r'^[A-Za-z]:[/\\]', raw)) or parsed.netloc or not parsed.path:
            continue
        raw_path = unquote(raw.split('#')[0]).replace('\\', '/')
        if Path(raw_path).suffix.lower() != '.md' and Path(raw_path).name not in MAIN_NAMES:
            continue
        basis, base, prefix = 'document', path.parent, ''
        declarations = []
        if kind == 'path_reference':
            for declaration in bases or []:
                if not all(isinstance(declaration.get(key), str) for key in ('path', 'prefix', 'base')):
                    raise RuleError('reference_basis', '路径基准须明确 path、prefix、base。')
                if Path(declaration['path']).resolve() == path and raw_path.startswith(declaration['prefix']):
                    location = Path.home() if declaration['base'] == 'home' else Path(declaration['base']).expanduser()
                    if not location.is_absolute():
                        raise RuleError('reference_basis', '路径基准须为 home 或已解析的绝对目录。')
                    declarations.append((declaration['prefix'], location))
        if declarations:
            declarations.sort(key=lambda item: len(item[0]), reverse=True)
            prefix, base = declarations[0]
            if any(p == prefix and b != base for p,b in declarations):
                raise RuleError('reference_basis', '同一引用前缀的路径基准冲突。')
            basis = 'declared'
        elif raw_path.startswith('~/'):
            basis, base = 'home', Path.home()
        elif Path(raw_path).is_absolute():
            basis = 'absolute'
        elif kind == 'path_reference' and raw_path.startswith(('.agents/', '.codex/', '.claude/')):
            basis = 'unresolved'
        target = None if basis == 'unresolved' else (base / Path(os.path.expanduser(raw_path))).resolve()
        result.append({'target': str(target) if target else None, 'basis': basis, 'base': str(base), 'prefix': prefix,
                       'raw': raw, 'label': label, 'kind': kind,
                       'start': start, 'end': end, 'fragment': parsed.fragment,
                       'line': content.count('\n', 0, start) + 1})
    return result


def reference_text(ref: dict[str, Any], target: Path, document: Path) -> str:
    from .maintenance import RuleError
    if ref['basis'] == 'unresolved':
        raise RuleError('reference_basis', '引用的路径基准待核实，请由 Agent 按文档声明提供 reference_bases。')
    if ref['basis'] == 'absolute':
        relative = str(target.resolve()).replace(os.sep, '/')
    else:
        base = Path(ref['base']) if ref['basis'] in ('declared', 'home') else document.resolve().parent
        expressed_target = target.resolve()
        if ref['basis'] in ('declared', 'home'):
            logical = base / Path(os.path.expanduser(unquote(ref['raw'].split('#')[0])))
            # Preserve a declared home-relative directory entrance even when it
            # resolves to a physical rule library elsewhere on the device.
            for anchor in logical.parents:
                if anchor.is_relative_to(base) and target.resolve().is_relative_to(anchor.resolve()):
                    expressed_target = anchor / target.resolve().relative_to(anchor.resolve())
                    break
        relative = os.path.relpath(expressed_target, base).replace(os.sep, '/')
        if ref['basis'] == 'declared' and not relative.startswith(ref['prefix']):
            raise RuleError('reference_basis', '新位置不再符合已声明的路径前缀，请单独核对引用方案。')
        if ref['basis'] == 'home':
            relative = '~/' + relative
    return quote(relative, safe='/.-_~:') + ('#' + ref['fragment'] if ref['fragment'] else '')


def index_project(project: Path, extra: list[str] | None = None, bases: list[dict[str, str]] | None = None) -> dict[str, Any]:
    from .maintenance import snapshot, RuleError, is_entrance
    pending = [Path(p) for p in extra or []]
    errors = []
    for directory, directories, files in os.walk(project, followlinks=False, onerror=lambda error: errors.append(str(error.filename))):
        directories[:] = [name for name in directories if name not in EXCLUDED and not is_entrance(Path(directory) / name)]
        pending.extend(Path(directory) / name for name in files if name in MAIN_NAMES)
    found: dict[str, dict[str, Any]] = {}
    while pending:
        path = pending.pop(0)
        key = os.path.normcase(str(path.absolute()))
        if key in found:
            continue
        try:
            record = snapshot(str(path))
            refs = references(path, record['content'], bases)
            record.update(role='main' if path.name in MAIN_NAMES else 'referenced',
                          scope=str(path.parent) if path.name in MAIN_NAMES else 'reference-defined',
                          references=refs, loading='unverified', status='found' if record['exists'] else 'missing')
            found[key] = record
            pending.extend(Path(ref['target']) for ref in refs if ref['target'])
        except (OSError, RuleError) as exc:
            found[key] = {'path': str(path), 'status': 'unreadable', 'error': type(exc).__name__, 'references': [], 'loading': 'unverified'}
    return {'project': str(project), 'files': sorted(found.values(), key=lambda record: record['path']),
            'errors': sorted(errors), 'loading': 'unverified'}


def scenario_changes(project: Path, data: dict[str, Any], indexed: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    from .maintenance import snapshot, RuleError, entity_key
    source = Path(data['path']).absolute()
    if data.get('scope') == 'global':
        if not data.get('rules_root'):
            raise RuleError('scope', '全局场景需明确物理规则根目录。')
        project = Path(data['rules_root']).resolve()
    if not source.is_relative_to(project):
        raise RuleError('scope', '场景操作须属于所选规则根目录。')
    if data.get('operation') == 'add':
        entry = Path(data['entry']).absolute()
        if source.exists() or source.is_symlink() or not entry.is_relative_to(project):
            raise RuleError('destination', '场景须尚未存在，主规则须属于同一管理范围。')
        if not all(isinstance(data.get(key), str) and data[key].strip() for key in ('content', 'title', 'trigger')):
            raise RuleError('reading_gate', '新增场景须提供正文、名称及读取触发条件。')
        main = snapshot(str(entry))
        relative = quote(os.path.relpath(source.resolve(), Path(main['entity']).parent).replace(os.sep, '/'), safe='/.-_')
        gate = f"\n- {data['trigger']}：读取并遵守 [{data['title']}]({relative})。\n"
        return [{'path': str(source), 'op': 'write', 'content': data['content']},
                {'path': str(entry), 'op': 'write', 'content': main['content'] + gate}]
    if data.get('operation') == 'edit':
        return [{'path': str(source), 'op': 'write', 'content': data['content']}]
    bases = data.get('reference_bases', [])
    indexed = indexed or index_project(project, [str(source), *data.get('entrances', [])], bases)
    if indexed['errors'] or any(record['status'] == 'unreadable' for record in indexed['files']):
        raise RuleError('references_unreadable', '相关入口或目录不可读，尚不能确定场景的全部已知引用。')
    if any(ref['basis'] == 'unresolved' for record in indexed['files'] for ref in record['references']):
        raise RuleError('reference_basis', '存在路径基准待核实的入口，请按文档声明提供 reference_bases 后重新计划。')
    original = snapshot(str(source))
    aliases = [record for record in indexed['files'] if Path(record['path']) != source and entity_key(record) == entity_key(original)]
    if any(record['kind'] != 'link' for record in aliases):
        raise RuleError('shared_entrance', '已知共享路径包含硬链接或目录映射，请先明确这些入口的逐项处理方案，保留当前实体。')
    def depth(record: dict[str, Any]) -> int:
        current = Path(record['path'])
        count = 0
        while current.is_symlink():
            count += 1
            if count > 128:
                raise RuleError('link_chain', '入口链接链过长，须单独核对。')
            current = Path(os.path.abspath(current.parent / os.readlink(current)))
        return count
    # Reconnect/delete dependent entrances before changing the links they follow.
    aliases.sort(key=depth, reverse=True)
    removed_paths = {str(source), *(record['path'] for record in aliases)}
    if data.get('operation') == 'delete':
        affected = [record for record in indexed['files']
                    if record['path'] not in removed_paths and any(Path(ref['target']) == source.resolve() for ref in record['references'])]
        by_path = {os.path.normcase(record['path']): record for record in affected}
        required = {entity_key(record): record for record in affected}
        replacements: dict[str, str] = {}
        for change in data.get('reference_changes', []):
            key = os.path.normcase(str(Path(change['path']).absolute()))
            if key not in by_path:
                raise RuleError('references_required', '引用修改须属于已列出的受影响文件。')
            identity = entity_key(by_path[key])
            content = change['content']
            if not isinstance(content, str):
                raise RuleError('candidate', '引用候选正文须为文本。')
            if identity in replacements and replacements[identity] != content:
                raise RuleError('reference_conflict', '同一实体的共享入口提供了不同候选，请先合成唯一正文。')
            replacements[identity] = content
        if set(replacements) != set(required):
            raise RuleError('references_required', '须同时提供以下引用实体的候选修改：' + '、'.join(record['path'] for record in required.values()))
        changes = []
        for identity, record in required.items():
            content = replacements[identity]
            revised = references(Path(record['path']), content, bases)
            if any(ref['target'] is None or Path(ref['target']) == source.resolve() for ref in revised):
                raise RuleError('broken_reference', '候选正文仍引用将删除的场景。')
            changes.append({'path': record['path'], 'op': 'write', 'content': content})
        changes.extend({'path': record['path'], 'op': 'delete'} for record in aliases)
        changes.append({'path': str(source), 'op': 'delete'})
        return changes
    if data.get('operation') != 'rename':
        raise RuleError('operation', '未知场景操作。')
    destination = Path(data['destination']).absolute()
    if not destination.is_relative_to(project) or destination.exists() or destination.is_symlink():
        raise RuleError('destination', '目标须为本项目中尚不存在的规则路径。')
    if not original['exists']:
        raise RuleError('missing', '待重命名场景不存在。')
    relocated = original['content']
    for ref in reversed(references(source, relocated, bases)):
        target = destination if Path(ref['target']) == source.resolve() else Path(ref['target'])
        relocated = relocated[:ref['start']] + reference_text(ref, target, destination) + relocated[ref['end']:]
    changes = [{'path': str(destination), 'op': 'write', 'content': relocated},
               *({'path': record['path'], 'op': 'link', 'target': str(destination)} for record in aliases),
               {'path': str(source), 'op': 'delete'}]
    updated_entities = set()
    for record in indexed['files']:
        if Path(record['path']).resolve() == source.resolve():
            continue
        matching = [ref for ref in record['references'] if Path(ref['target']) == source.resolve()]
        if not matching:
            continue
        identity = entity_key(record)
        if identity in updated_entities:
            continue
        updated_entities.add(identity)
        content = record['content']
        for ref in reversed(matching):
            replacement = reference_text(ref, destination, Path(record['entity']))
            content = content[:ref['start']] + replacement + content[ref['end']:]
        changes.append({'path': record['path'], 'op': 'write', 'content': content})
    return changes
