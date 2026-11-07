"""Bounded, read-only discovery. A candidate path never proves session loading."""
from __future__ import annotations
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import tomllib
from typing import Any

SUPPORTED = {'codex': ('Codex', 'codex', '@openai/codex'),
             'claude-code': ('Claude Code', 'claude', '@anthropic-ai/claude-code'),
             'workbuddy': ('WorkBuddy', 'workbuddy', '@genie/agent-cli'),
             'hermes': ('Hermes', 'hermes', None), 'opencode': ('OpenCode', 'opencode', 'opencode-ai')}
UNSUPPORTED = {'gemini-cli': 'Gemini CLI', 'github-copilot': 'GitHub Copilot', 'cursor': 'Cursor',
               'windsurf': 'Windsurf', 'cline': 'Cline', 'openclaw': 'OpenClaw'}
IDENTITIES = {'SOUL.md', 'IDENTITY.md', 'USER.md', 'MEMORY.md', 'BOOTSTRAP.md'}
MECHANISMS = {
 'codex': {'source': 'https://developers.openai.com/codex/guides/agents-md/',
           'checked': '2026-11-07', 'scope': 'Current official documentation; installed session not verified',
           'summary': 'CODEX_HOME；全局和每层项目首个非空 override / AGENTS / 已配置回退；项目目录链有总字节预算。'},
 'claude-code': {'source': 'https://code.claude.com/docs/en/memory#agents-md',
                 'checked': '2026-11-05', 'scope': 'Workspace investigation of 2.1.312; native AGENTS requires >=2.1.277',
                 'summary': '用户 CLAUDE 与 rules；项目 CLAUDE、.claude/CLAUDE、local、条件 rules。原生 AGENTS 受版本及 projectInstructions 模式影响；项目 CLAUDE 可抑制默认 AGENTS 读取。'},
 'workbuddy': {'source': 'WorkBuddy 5.9.3 app.asar main/authority-snapshot.js; CLI 2.165.0 MemoryLoader',
               'checked': '2026-11-07', 'scope': 'Only examined build; other versions require verification',
               'summary': '用户 CODEBUDDY.md/.mdc 与 rules；项目和 local 独立加载。SOUL/IDENTITY/USER 属于 persona；账户目录及实际会话需另核对。'},
 'hermes': {'source': 'https://github.com/NousResearch/hermes-agent/blob/b200f5bc60e31588ba2cb60e288029c4a54ba5fa/agent/prompt_builder.py',
            'checked': '2026-11-07', 'scope': 'Inspected commit plus hermes_constants.py and hermes_cli/profiles.py',
            'summary': 'Windows 默认 LOCALAPPDATA/hermes，其他平台 ~/.hermes；home 和 Profile 可覆盖。SOUL 为身份。项目按 Hermes、AGENTS 链、CLAUDE、Cursor 选择一种类型。'},
 'opencode': {'source': 'https://github.com/anomalyco/opencode/blob/v1.19.5/packages/opencode/src/session/instruction.ts',
              'checked': '2026-11-07', 'scope': '1.19.5 source; flags/config can change selected files',
              'summary': 'XDG 配置 AGENTS 存在即优先，否则可回退 ~/.claude/CLAUDE；禁用 Claude prompt 时不回退。项目首种匹配及配置 instructions 需分别核对。'}
}


def rule(path: Path, role: str, profile: str, selection: str = 'candidate') -> dict[str, Any]:
    from .maintenance import snapshot, RuleError
    try:
        record = snapshot(str(path))
        record.update(status='found' if record['exists'] else 'missing', entity_status='resolved')
    except (OSError, RuleError, ValueError) as exc:
        record = {'path': str(path), 'status': 'unreadable', 'entity_status': 'unknown', 'error': type(exc).__name__}
    record.update(role=role, profile=profile, selection=selection, loading='unverified', behavior='unverified')
    return record


def json_config(path: Path) -> dict[str, Any]:
    # Strict JSON is read without ever exposing unrelated host secrets. JSONC stays
    # explicitly unverified, rather than applying a regex that can corrupt URLs.
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    return value if isinstance(value, dict) else {}


def launcher_target(path: Path, environment: dict[str, str]) -> Path | None:
    """Recognize one literal Windows forwarding command; never execute a wrapper."""
    if path.suffix.lower() != '.cmd':
        return None
    try:
        if path.stat().st_size > 8192:
            return None
        body = path.read_text(encoding='utf-8-sig')
        match = re.fullmatch(r'\s*@?echo off\s*\n\s*"([^"\r\n]+\.exe)"\s+%\*\s*', body, re.IGNORECASE)
        if not match:
            return None
        variables = {key.upper(): value for key,value in environment.items()}
        target = re.sub(r'%([^%]+)%', lambda value: variables.get(value[1].upper(), value[0]), match[1])
        if '%' in target or not Path(target).is_absolute():
            return None
        return Path(target).resolve()
    except (OSError, UnicodeError):
        return None


def native_installs() -> tuple[list[dict[str, Any]], list[str]]:
    found: list[dict[str, Any]] = []
    errors = []
    if sys.platform != 'win32':
        return found, errors
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for location in ('Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall',
                         'Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall'):
            try:
                with winreg.OpenKey(hive, location) as parent:
                    for index in range(winreg.QueryInfoKey(parent)[0]):
                        try:
                            with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as entry:
                                name = str(winreg.QueryValueEx(entry, 'DisplayName')[0])
                                product = next((key for key, info in SUPPORTED.items() if info[0].replace(' ', '').lower() in name.replace(' ', '').lower()), None)
                                if not product:
                                    continue
                                def field(key: str) -> str | None:
                                    try:
                                        return str(winreg.QueryValueEx(entry, key)[0])
                                    except OSError:
                                        return None
                                icon = field('DisplayIcon')
                                path = field('InstallLocation') or (re.sub(r',-?\d+$', '', icon).strip('"') if icon else None)
                                found.append({'product': product, 'path': path, 'version': field('DisplayVersion'), 'source': 'Windows uninstall record'})
                        except OSError:
                            continue
            except FileNotFoundError:
                pass
            except OSError:
                errors.append('Windows uninstall records unreadable')
    script = ('[Console]::OutputEncoding=[Text.Encoding]::UTF8; '
              'Get-AppxPackage *Codex* | Select-Object Name,Version,InstallLocation | ConvertTo-Json -Compress')
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, timeout=15, encoding='utf-8', errors='replace',
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            errors.append('Windows Appx query failed')
        elif result.stdout.strip():
            records = json.loads(result.stdout)
            for item in records if isinstance(records, list) else [records]:
                found.append({'product': 'codex', 'path': item['InstallLocation'], 'version': item['Version'], 'source': 'Windows Appx record'})
    except (OSError, ValueError, subprocess.TimeoutExpired):
        errors.append('Windows Appx query unavailable')
    return found, errors


def scan(project: Path, environment: dict[str, str], data: dict[str, Any]) -> dict[str, Any]:
    from .maintenance import RuleError, digest
    from .indexing import index_project
    home = Path(data.get('home') or Path.home()).absolute()
    local = Path(environment.get('LOCALAPPDATA') or home / 'AppData/Local')
    config = Path(environment.get('XDG_CONFIG_HOME') or home / '.config')
    hermes_default = ((local / ('hermes' + environment.get('HERMES_DATA_DIR_SUFFIX', ''))) if sys.platform == 'win32'
                      else home / ('.hermes' + environment.get('HERMES_DATA_DIR_SUFFIX', '')))
    roots = {'codex': Path(environment.get('CODEX_HOME') or home / '.codex'),
             'claude-code': Path(environment.get('CLAUDE_CONFIG_DIR') or home / '.claude'),
             'workbuddy': Path(environment.get('WORKBUDDY_CONFIG_DIR') or environment.get('CODEBUDDY_CONFIG_DIR') or home / '.workbuddy'),
             'hermes': Path(environment.get('HERMES_HOME') or hermes_default), 'opencode': config / 'opencode'}
    custom = data.get('custom', [])
    if not isinstance(custom, list) or any(not isinstance(x, dict) or x.get('product') not in SUPPORTED for x in custom):
        raise RuleError('product', '显式位置仅支持首版五种产品。')
    native, errors = native_installs() if data.get('probe_system', True) else ([], [])
    products = []
    for key, (name, command, package) in SUPPORTED.items():
        candidates: list[dict[str, Any]] = [x for x in native if x['product'] == key]
        for directory in environment.get('PATH', '').split(os.pathsep):
            if not directory or not Path(directory).is_absolute():
                continue
            for suffix in (('.exe', '.cmd', '.ps1', '') if os.name == 'nt' else ('',)):
                path = Path(directory) / (command + suffix)
                try:
                    present = path.is_file()
                except OSError:
                    errors.append(f'{key}: installation path unreadable: {path}')
                    candidates.append({'path': str(path), 'version': None, 'source': 'PATH file'})
                    continue
                if not present:
                    continue
                metadata = Path(directory) / 'node_modules' / str(package) / 'package.json' if package else None
                version = None
                instance = str(path.resolve())
                if metadata:
                    try:
                        if not metadata.is_file():
                            raise FileNotFoundError()
                        version = json_config(metadata).get('version')
                        instance = str(metadata.parent.resolve())
                    except FileNotFoundError:
                        pass
                    except (OSError, ValueError):
                        errors.append(f'{key}: package metadata unreadable')
                candidates.append({'path': str(path), 'instance': instance, 'version': version, 'source': 'PATH + package metadata' if version else 'PATH file'})
        known: list[Path] = []
        if os.name == 'nt':
            if key == 'codex':
                known = [local / 'Programs/OpenAI/Codex/bin/codex.exe', home / '.local/bin/codex.exe']
            elif key == 'claude-code':
                known = [home / '.local/bin/claude.exe']
            elif key == 'workbuddy':
                known = [Path(environment.get('ProgramFiles', 'C:/Program Files')) / 'WorkBuddy/WorkBuddy.exe'] if data.get('probe_system', True) else []
            elif key == 'hermes':
                known = [hermes_default / 'bin/hermes.exe']
        else:
            known = [home / '.local/bin' / command, home / '.opencode/bin/opencode' if key == 'opencode' else home / '.local/bin' / command]
            if data.get('probe_system', True):
                known += [Path('/Applications') / (name.replace(' Code', '') + '.app'), home / 'Applications' / (name.replace(' Code', '') + '.app')]
        for path in known:
            try:
                present = path.exists()
            except OSError:
                errors.append(f'{key}: known installation path unreadable: {path}')
                present = True
            if present:
                candidates.append({'path': str(path), 'version': None, 'source': 'known installation path'})
        candidates += [{'path': x['installation'], 'version': None, 'source': 'explicit installation'} for x in custom if x['product'] == key and x.get('installation')]
        installs = []
        seen: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            path_value = candidate.get('path')
            if not path_value:
                candidate.update(status='record_only', entity=None)
                installs.append(candidate)
                continue
            path = Path(path_value).absolute()
            try:
                target = launcher_target(path, environment)
                resolved = str(target or path.resolve())
                identity = os.path.normcase(str(target) if target else candidate.get('instance') or resolved)
            except OSError:
                resolved = str(path)
                identity = os.path.normcase(str(path))
            try:
                path.stat()
                status = 'found'
            except FileNotFoundError:
                status = 'missing'
            except OSError:
                status = 'unreadable'
            entrance = {'path':str(path), 'source':candidate['source'], 'status':status}
            if identity in seen:
                if entrance not in seen[identity]['entrances']:
                    seen[identity]['entrances'].append(entrance)
                if status == 'found':
                    seen[identity]['status'] = 'found'
                continue
            candidate.update(path=str(path), entity=resolved, id=digest([key, identity])[:16], status=status,
                             entrances=[entrance], runnable='unverified',
                             instance_evidence='package metadata' if candidate.get('version') else 'resolved entry; installation relationship not fully verified')
            seen[identity] = candidate
            installs.append(candidate)
        profiles: list[dict[str, Any]] = [{'name': 'default', 'home': str(roots[key].absolute()), 'source': 'environment or platform default', 'current_session': 'unverified'}]
        rules: list[dict[str, Any]] = []
        configured_models = []
        if key == 'codex':
            configuration = roots[key] / 'config.toml'
            try:
                values = tomllib.loads(configuration.read_text(encoding='utf-8-sig'))
                if values.get('model'):
                    configured_models.append({'profile': 'default', 'id': values['model'], 'source': str(configuration)})
                for profile, settings in values.get('profiles', {}).items():
                    profiles.append({'name': profile, 'home': str(roots[key]), 'source': str(configuration), 'current_session': 'unverified'})
                    if settings.get('model'):
                        configured_models.append({'profile': profile, 'id': settings['model'], 'source': str(configuration)})
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                errors.append('codex: config.toml unreadable; profile/model unknown')
            # CLI 0.166 --profile layers <name>.config.toml. Older inspected builds
            # expose [profiles.*]; report both sources without selecting a session.
            for profile_file in roots[key].glob('*.config.toml'):
                try:
                    profile_name = profile_file.name.removesuffix('.config.toml')
                    settings = tomllib.loads(profile_file.read_text(encoding='utf-8-sig'))
                    profiles.append({'name': profile_name, 'home': str(roots[key]), 'source': str(profile_file), 'current_session': 'unverified'})
                    if settings.get('model'):
                        configured_models.append({'profile': profile_name, 'id': settings['model'], 'source': str(profile_file)})
                except (OSError, ValueError):
                    errors.append(f'codex: named profile {profile_file.name} unreadable')
        if key in ('hermes', 'workbuddy'):
            base = roots[key] / 'profiles' if key == 'hermes' else roots[key]
            try:
                for path in base.iterdir():
                    if path.is_dir() and not path.is_symlink() and not path.is_junction() and (key == 'hermes' or path.name.startswith('user-')):
                        profiles.append({'name': path.name, 'home': str(path), 'source': 'known profile directory', 'current_session': 'unverified'})
            except FileNotFoundError:
                pass
            except OSError:
                errors.append(f'{key}: profiles unreadable')
            if key == 'hermes':
                marker = roots[key] / 'active_profile'
                try:
                    active = marker.read_text(encoding='utf-8-sig').strip()
                    for profile in profiles:
                        profile['configured_active'] = profile['name'] == active
                except FileNotFoundError:
                    pass
                except (OSError, UnicodeError):
                    errors.append('hermes: active profile unreadable')
        profiles += [{'name': x.get('profile', 'custom'), 'home': str(Path(x['home']).absolute()), 'source': 'explicit home', 'current_session': 'unverified'} for x in custom if x['product'] == key and x.get('home')]
        for profile in profiles:
            root, profile_name = Path(profile['home']), profile['name']
            if key == 'codex':
                selected = False
                for filename in ('AGENTS.override.md', 'AGENTS.md'):
                    item = rule(root / filename, 'main', profile_name)
                    item['selection'] = 'shadowed' if selected else 'candidate'
                    if item.get('content', '').strip() and not selected:
                        item['selection'] = 'mechanism_candidate'
                        selected = True
                    rules.append(item)
            elif key in ('claude-code', 'workbuddy'):
                main = 'CLAUDE.md' if key == 'claude-code' else 'CODEBUDDY.md'
                rules.append(rule(root / main, 'main', profile_name))
                if key == 'workbuddy':
                    rules.extend(rule(root / filename, 'identity' if filename != 'MEMORY.md' else 'memory', profile_name) for filename in ('SOUL.md', 'IDENTITY.md', 'USER.md', 'MEMORY.md'))
                rules_directory = root / 'rules'
                try:
                    for folder, dirs, files in os.walk(rules_directory, followlinks=False):
                        dirs[:] = [d for d in dirs if not (Path(folder) / d).is_symlink() and not (Path(folder) / d).is_junction()]
                        rules.extend(rule(Path(folder)/filename, 'scenario', profile_name, 'conditional') for filename in files if filename.endswith(('.md', '.mdc')))
                except OSError:
                    errors.append(f'{key}: rules directory unreadable')
            elif key == 'hermes':
                rules.append(rule(root / 'SOUL.md', 'identity', profile_name))
            elif key == 'opencode':
                own = rule(root / 'AGENTS.md', 'main', profile_name)
                rules.append(own)
                disabled = any(environment.get(flag, '').lower() in ('1','true') for flag in ('OPENCODE_DISABLE_CLAUDE_CODE','OPENCODE_DISABLE_CLAUDE_CODE_PROMPT'))
                selection = 'disabled' if disabled else 'shadowed' if own['status'] == 'found' else 'fallback_candidate' if own['status'] == 'missing' else 'unknown'
                rules.append(rule(home / '.claude/CLAUDE.md', 'main', profile_name, selection))
                for filename in ('opencode.json', 'opencode.jsonc'):
                    path = root / filename
                    try:
                        settings = json_config(path)
                        if settings.get('model'):
                            configured_models.append({'profile': profile_name, 'id': settings['model'], 'source': str(path)})
                        for instruction in settings.get('instructions', []):
                            if not isinstance(instruction, str):
                                continue
                            if re.match(r'https?://', instruction) or any(c in instruction for c in '*?{'):
                                rules.append({'path': instruction, 'role': 'referenced', 'status': 'unverified', 'selection': 'config_expression', 'source': str(path), 'loading': 'unverified'})
                            else:
                                rules.append(rule(Path(os.path.expanduser(instruction)) if Path(instruction).is_absolute() else project/instruction, 'referenced', profile_name, 'config_candidate'))
                    except FileNotFoundError:
                        pass
                    except (OSError, ValueError):
                        errors.append(f'opencode: {filename} requires configuration review (JSONC or unreadable)')
        for item in custom:
            if item['product'] == key:
                rules.extend(rule(Path(path).absolute(), item.get('role', 'unknown'), item.get('profile', 'custom'), 'explicit') for path in item.get('paths', []))
        products.append({'id': key, 'name': name, 'supported': True,
                         'installation_status': 'found' if any(i['status'] == 'found' for i in installs) else 'unreadable' if any(i['status'] == 'unreadable' for i in installs) else 'not_found',
                         'installations': installs, 'profiles': profiles, 'rules': rules, 'mechanism': MECHANISMS[key],
                         'models': {'configured': configured_models, 'current': {'id': None, 'source': 'unknown'}, 'targets': []},
                         'loading': 'unverified', 'behavior': 'unverified'})
    products.extend({'id': key, 'name': name, 'supported': False, 'installation_status': 'unsupported'} for key,name in UNSUPPORTED.items())
    return {'environment': {'platform': sys.platform, 'release': platform.release(), 'home': str(home), 'kind': 'native', 'detected_at': time.time()},
            'products': products, 'project_rules': index_project(project), 'errors': errors,
            'boundary': 'Current native user, PATH, known installation records and explicit paths; no disk, WSL or remote scan.'}
