"""The Skill's ordinary configuration layers; no host configuration writes."""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any
import uuid

KEYS = {'history_keep': 'AGENT_RULES_HISTORY_KEEP', 'binding': 'AGENT_RULES_BINDINGS',
        'review_models': 'AGENT_RULES_REVIEW_MODELS'}
PRODUCTS = {'codex', 'claude-code', 'workbuddy', 'hermes', 'opencode'}


def parse_env(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = (part.strip() for part in line.split('=', 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[key] = value
    return values


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Settings:
    def __init__(self, personal: Path, project: Path, environment: dict[str, str], use_personal: bool):
        self.personal, self.project = personal, project
        self.environment, self.use_personal = environment, use_personal

    def _layers(self) -> list[tuple[str, dict[str, str]]]:
        layers = [('process', {key: self.environment[key] for key in KEYS.values() if key in self.environment})]
        files = [self.project / '.env.agent-instructions-update', self.project / '.env.local', self.project / '.env']
        if self.use_personal:
            files.append(self.personal / '.env')
        for path in files:
            try:
                content = path.read_text(encoding='utf-8-sig')
            except FileNotFoundError:
                content = ''
            except (OSError, UnicodeError):
                from .maintenance import RuleError
                raise RuleError('config_unreadable', f'无法读取配置来源：{path}') from None
            layers.append((str(path), parse_env(content)))
        return layers

    def validate_binding(self, value: Any, source: str) -> dict[str, Any]:
        from .maintenance import RuleError
        required = {'scope', 'project', 'product', 'installation', 'profile', 'paths', 'authority', 'layout'}
        if not isinstance(value, dict) or not required <= set(value):
            raise RuleError('binding_incomplete', f'目标绑定字段不完整，不能跨层补齐：{source}')
        if value['scope'] not in ('global', 'project') or value['product'] not in {*PRODUCTS, None} or not isinstance(value['paths'], list):
            raise RuleError('binding_invalid', f'目标绑定无效：{source}')
        if value['scope'] == 'project' and value['project'] != str(self.project):
            raise RuleError('binding_project', '绑定属于另一个项目。')
        if value['scope'] == 'global' and value['project'] is not None:
            raise RuleError('binding_project', '全局绑定的 project 须为 null。')
        if any(value[key] is not None and (not isinstance(value[key], str) or not value[key].strip())
               for key in ('installation', 'profile', 'authority', 'layout')):
            raise RuleError('binding_invalid', '安装、Profile、权威与布局须为非空文本，未核实值使用 null。')
        if not all(isinstance(path, str) and path.strip() for path in value['paths']):
            raise RuleError('binding_invalid', '规则路径须为字符串列表。')
        # Relative project bindings always resolve against that project. Global bindings
        # resolve against the personal configuration directory, never an installation.
        base = self.project if value['scope'] == 'project' else self.personal
        normalized = dict(value)
        normalized['paths'] = [str((base / Path(os.path.expanduser(p))).absolute()) for p in value['paths']]
        if value['authority']:
            normalized['authority'] = str((base / Path(os.path.expanduser(value['authority']))).absolute())
            if normalized['authority'] not in normalized['paths']:
                raise RuleError('binding_paths', '绑定的权威文件必须包含在 paths 中。')
        return normalized

    def read(self, scope: str | None = None) -> dict[str, Any]:
        from .maintenance import digest, RuleError
        selected_scope = scope or str(self.project)
        if selected_scope not in ('global', str(self.project)):
            raise RuleError('scope', '配置范围须为全局或当前项目。')
        layers = self._layers()
        values: dict[str, Any] = {'history_keep': 30, 'binding': None, 'review_models': []}
        sources: dict[str, Any] = {key: 'default' for key in values}
        try:
            for field, key in KEYS.items():
                for source, items in layers:
                    raw = items.get(key, '')
                    if not raw.strip():
                        continue
                    if field == 'history_keep':
                        parsed = int(raw)
                        if parsed < 1:
                            raise ValueError()
                        values[field] = parsed
                    else:
                        mapping = json.loads(raw)
                        if not isinstance(mapping, dict):
                            raise ValueError()
                        if selected_scope not in mapping:
                            continue
                        record = mapping[selected_scope]
                        if field == 'binding':
                            values[field] = self.validate_binding(record, source)
                        else:
                            if not isinstance(record, list) or not all(isinstance(x, str) and x for x in record):
                                raise ValueError()
                            values[field] = record
                    sources[field] = source
                    break
        except (ValueError, TypeError):
            raise RuleError('config_invalid', '配置值无效；检查历史数量或结构化 JSON，不能静默回退。') from None
        return {'values': values, 'sources': sources, 'scope': selected_scope,
                'revision': digest([selected_scope, layers]), 'personal_enabled': self.use_personal,
                'save_locations': {'personal': str(self.personal / '.env'), 'project': str(self.project / '.env.local')}}

    def save(self, data: dict[str, Any]) -> dict[str, Any]:
        from .maintenance import RuleError
        current = self.read(data.get('scope'))
        if current['revision'] != data.get('revision'):
            raise RuleError('config_changed', '配置来源已变化，请刷新后再保存。')
        save_scope = data.get('save_scope', 'personal')
        if save_scope not in ('personal', 'project'):
            raise RuleError('config_scope', '保存位置须为个人或当前项目。')
        if save_scope == 'personal' and not self.use_personal:
            raise RuleError('config_disabled', '本次未启用个人设置。')
        path = Path(current['save_locations'][save_scope])
        if path.is_symlink():
            raise RuleError('config_link', '配置保存目标是链接，须先核对实际位置。')
        content = path.read_text(encoding='utf-8-sig') if path.exists() else ''
        original = parse_env(content)
        updates = {}
        for field, value in data.get('values', {}).items():
            if field not in KEYS:
                raise RuleError('config_key', '未支持的配置字段。')
            if field == 'history_keep':
                if type(value) is not int or value < 1:
                    raise RuleError('retention', '完成任务保留数量须为正整数。')
                updates[KEYS[field]] = str(value)
            else:
                if field == 'binding':
                    self.validate_binding(value, str(path))
                elif not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
                    raise RuleError('models', '目标模型须为模型 ID 列表。')
                try:
                    mapping = json.loads(original.get(KEYS[field], '{}'))
                    mapping[current['scope']] = value
                except (ValueError, TypeError):
                    raise RuleError('config_invalid', f'目标文件已有结构化配置损坏：{path}') from None
                updates[KEYS[field]] = json.dumps(mapping, ensure_ascii=False, separators=(',', ':'))
        lines = [line for line in content.splitlines() if line.partition('=')[0].strip() not in updates]
        lines.extend(f'{key}={value}' for key, value in updates.items())
        if self.read(data.get('scope'))['revision'] != current['revision']:
            raise RuleError('config_changed', '写入前配置来源已变化。')
        atomic_text(path, '\n'.join(lines) + '\n')
        result = self.read(data.get('scope'))
        result['saved_to'] = str(path)
        result['overridden'] = [key for key in data.get('values', {}) if result['sources'][key] != str(path)]
        return result
