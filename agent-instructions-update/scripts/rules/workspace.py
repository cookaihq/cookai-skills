"""Resolve launch context and expose rules through the maintenance boundary."""
from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Any
from .configuration import Settings
from .indexing import EXCLUDED, MAIN_NAMES, index_project, scenario_title


def project_context(directory: Path) -> dict[str, Any]:
    errors: list[str] = []
    selected = directory.resolve()
    identified = False
    for candidate in (selected, *selected.parents):
        if candidate == Path.home() or candidate.parent == candidate:
            break
        try:
            names = {p.name for p in candidate.iterdir()}
            if '.git' in names or names & MAIN_NAMES:
                selected = candidate
                identified = True
                break
        except OSError as exc:
            errors.append(f'{candidate}: {type(exc).__name__}')
            break
    return {'path': str(selected), 'name': selected.name, 'launch': str(directory), 'errors': errors, 'identified':identified}


def scenario_paths(root: Path) -> tuple[list[str], list[str]]:
    paths: list[str] = []
    errors: list[str] = []
    try:
        root.stat()
    except FileNotFoundError:
        return paths, errors
    except OSError as exc:
        return paths, [f'{root}: {type(exc).__name__}']
    for directory, directories, files in os.walk(root, followlinks=False, onerror=lambda exc: errors.append(str(exc.filename))):
        directories[:] = [d for d in directories if d not in EXCLUDED and not (Path(directory)/d).is_symlink() and not (Path(directory)/d).is_junction()]
        paths.extend(str(Path(directory)/f) for f in files if f.endswith(('.md', '.mdc')))
    return paths, errors


def reading_gate(content: str, line_number: int) -> bool:
    lines = content.splitlines()
    offset = line_number - 1
    context = [lines[offset]]
    start, end = offset, offset
    while start >= 0 and re.search(r'(?<!\\)\|', lines[start]):
        start -= 1
    while end < len(lines) and re.search(r'(?<!\\)\|', lines[end]):
        end += 1
    if any(all(re.fullmatch(r'\s*:?-+:?\s*', cell) for cell in row.strip().strip('|').split('|'))
           for row in lines[start + 1:end]):
        offset = start
        while offset >= 0 and not lines[offset].strip():
            offset -= 1
        while offset >= 0 and lines[offset].strip() and not lines[offset].lstrip().startswith('#'):
            context.append(lines[offset])
            offset -= 1
    return re.search(r'读取|遵守|\bread\b|\bfollow\b', '\n'.join(context), re.IGNORECASE) is not None


def read_workspace(app: Any, data: dict[str, Any]) -> dict[str, Any]:
    registered = app.settings.read()['values']['binding']
    context = ({'path':str(app.project), 'name':app.project.name, 'launch':str(app.project),
                'errors':[], 'identified':True} if registered else project_context(app.project))
    project = Path(context['path'])
    settings = app.settings if project == app.project else Settings(app.personal, project, app.settings.environment, app.settings.use_personal)
    binding = settings.read()['values']['binding']
    scenario_root = project/'docs/agents'
    paths, errors = scenario_paths(scenario_root)
    if binding:
        paths.extend(binding['paths'])
    indexed = index_project(project, paths)
    if binding and binding['authority']:
        for record in indexed['files']:
            if record['path'] == binding['authority']:
                record['role'] = 'main'
    context['errors'] += indexed['errors'] + errors
    main = [record for record in indexed['files'] if record.get('role') == 'main']
    context['availability'] = 'error' if context['errors'] or any(x['status']=='unreadable' for x in indexed['files']) else 'ready' if any(x['status']=='found' for x in main) else 'empty' if context['identified'] else 'none'
    root = project
    discovery = None
    extras: dict[str, dict[str, Any]] = {}
    if data.get('scope') == 'global':
        from .discovery import scan
        home_path = app.settings.environment.get('USERPROFILE') or app.settings.environment.get('HOME') or str(Path.home())
        discovery = scan(project, app.settings.environment, {'home':home_path, 'probe_system':False})
        home = Path(discovery['environment']['home'])
        binding = app.settings.read('global')['values']['binding']
        root = Path(data['rules_root']).expanduser().resolve() if data.get('rules_root') else Path(binding['authority']).resolve().parent if binding and binding['authority'] else (home/'.agents').resolve()
        scenario_root = root/'scenarios-agent-instructions'
        paths, errors = scenario_paths(scenario_root)
        paths.extend(str(root/name) for name in MAIN_NAMES if (root/name).exists())
        if binding:
            paths.extend(binding['paths'])
        for product in discovery['products']:
            for record in product.get('rules', []):
                if record.get('status') in ('found', 'missing', 'unreadable'):
                    extras.setdefault(record['path'], {**record, 'product':product['id'], 'product_name':product['name']})
                    if record.get('role') in ('main', 'scenario') and record['status']=='found':
                        paths.append(record['path'])
        indexed = index_project(root, list(dict.fromkeys(paths)), data.get('reference_bases'), scan_root=False)
        indexed['errors'] += errors
        for record in indexed['files']:
            details = extras.get(record['path'], {})
            record.update({key:details[key] for key in ('product','product_name','profile','selection','role') if key in details})
        present = {x['path'] for x in indexed['files']}
        indexed['files'].extend({**x, 'title':scenario_title(x.get('content','')), 'references':[]} for path,x in extras.items() if path not in present)
        main = [record for record in indexed['files'] if record.get('role') == 'main']
    reading_targets = {ref['target'] for record in indexed['files'] for ref in record.get('references', [])
                       if ref.get('target') and reading_gate(record.get('content',''), ref['line'])}
    scenarios = []
    for record in indexed['files']:
        if record.get('role') in ('main','identity','memory'):
            continue
        path = Path(record['path'])
        if not path.is_relative_to(scenario_root) and record.get('role') != 'scenario' and str(path.resolve()) not in reading_targets:
            continue
        relative = path.relative_to(scenario_root).as_posix() if path.is_relative_to(scenario_root) else path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
        scenarios.append({**record, 'relative_path':relative})
    return {'project':context, 'scope':'global' if data.get('scope')=='global' else str(project), 'root':str(root), 'scenario_root':str(scenario_root),
            'main_files':main, 'scenarios':scenarios, 'files':indexed['files'], 'discovery':discovery, 'errors':indexed['errors']}
