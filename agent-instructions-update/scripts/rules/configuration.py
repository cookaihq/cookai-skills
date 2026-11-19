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
    from .maintenance import RuleError
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise RuleError('config_write', f'配置尚未提交；请检查文件及目录权限：{path}') from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class Settings:
    def __init__(self, personal: Path, project: Path, environment: dict[str, str], use_personal: bool):
        self.personal, self.project = personal, project
        self.environment, self.use_personal = environment, use_personal

    def _layers(self, tolerate_errors=False) -> list[tuple[str, dict[str, str]]]:
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
                if tolerate_errors:
                    layers.append((str(path), None))
                    continue
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

    def read(self, scope: str | None = None, *, tolerate_errors=False) -> dict[str, Any]:
        from .maintenance import digest, RuleError
        selected_scope = scope or str(self.project)
        if selected_scope not in ('global', str(self.project)):
            raise RuleError('scope', '配置范围须为全局或当前项目。')
        layers = self._layers(tolerate_errors)
        values: dict[str, Any] = {'history_keep': 30, 'binding': None, 'review_models': []}
        sources: dict[str, Any] = {key: 'default' for key in values}
        errors = {}
        for field, key in KEYS.items():
            try:
                for source, items in layers:
                    if items is None:
                        raise RuleError('config_unreadable', f'无法读取配置来源：{source}')
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
                            values[field] = None if record is None else self.validate_binding(record, source)
                        else:
                            if not isinstance(record, list) or not all(isinstance(x, str) and x for x in record):
                                raise ValueError()
                            values[field] = record
                    sources[field] = source
                    break
            except (ValueError, TypeError, RuleError) as error:
                if not tolerate_errors:
                    if isinstance(error, RuleError):
                        raise
                    raise RuleError('config_invalid', f'{key} 配置无效：{source}') from None
                values[field], sources[field] = None, source
                errors[field] = getattr(error, 'message', f'{key} 配置值无效：{source}')
        update_path = self.personal/'.env'
        try:
            update_text = update_path.read_text(encoding='utf-8-sig')
        except FileNotFoundError:
            update_text = ''
        except (OSError, UnicodeError):
            if not tolerate_errors:
                raise RuleError('config_unreadable', f'无法读取更新偏好：{update_path}') from None
            update_text = ''
            errors['auto_update'] = f'无法读取更新偏好：{update_path}'
        update_flag = parse_env(update_text).get('AUTO_UPDATE_CHECK', '1')
        if update_flag not in ('0', '1'):
            if not tolerate_errors:
                raise RuleError('config_invalid', 'AUTO_UPDATE_CHECK 须为 0 或 1。')
            errors['auto_update'] = 'AUTO_UPDATE_CHECK 须为 0 或 1。'
        values['auto_update'] = None if 'auto_update' in errors else update_flag != '0'
        sources['auto_update'] = str(update_path) if 'AUTO_UPDATE_CHECK' in parse_env(update_text) else 'default'
        revisions = {field: digest([selected_scope, [[source, items.get(key) if items is not None else 'unreadable']
            for source, items in layers], errors.get(field)]) for field, key in KEYS.items()}
        revisions['auto_update'] = digest([update_flag, errors.get('auto_update')])
        return {'values': values, 'sources': sources, 'scope': selected_scope, 'errors': errors, 'field_revisions': revisions,
                'revision': digest([selected_scope, layers, update_text]), 'personal_enabled': self.use_personal,
                'save_locations': {'personal': str(self.personal / '.env'), 'project': str(self.project / '.env.local')}}

    def save(self, data: dict[str, Any]) -> dict[str, Any]:
        from .maintenance import RuleError
        from skill_ui.storage import file_lock
        current = self.read(data.get('scope'), tolerate_errors=True)
        values = data.get('values')
        if not isinstance(values, dict) or not values or set(values) - {*KEYS, 'auto_update'}:
            raise RuleError('config_key', '请提交有效的配置字段。')
        save_scope = data.get('save_scope', 'personal')
        if save_scope not in ('personal', 'project'):
            raise RuleError('config_scope', '保存位置须为个人或当前项目。')
        if save_scope == 'personal' and not self.use_personal:
            raise RuleError('config_disabled', '本次未启用个人设置。')
        path = Path(current['save_locations'][save_scope])
        if path.is_symlink():
            raise RuleError('config_link', '配置保存目标是链接，须先核对实际位置。')
        path.parent.mkdir(parents=True, exist_ok=True)
        results = {}
        with file_lock(path.with_name(path.name + '.lock')):
            current = self.read(data.get('scope'), tolerate_errors=True)
            try:
                content = path.read_text(encoding='utf-8-sig') if path.exists() else ''
            except (OSError, UnicodeError):
                raise RuleError('config_unreadable', f'无法安全更新目标文件：{path}') from None
            original, updates = parse_env(content), {}
            for field, value in values.items():
                try:
                    expected = data.get('field_revisions')
                    if ((expected is None and current['revision'] != data.get('revision')) or
                            expected is not None and expected.get(field) != current['field_revisions'][field]):
                        raise RuleError('config_changed', '本项来源已变化；草稿保留，请重读并核对。')
                    if field == 'auto_update':
                        if save_scope != 'personal' or type(value) is not bool:
                            raise RuleError('update_scope', '更新开关仅保存到个人设置，值须为布尔值。')
                        updates['AUTO_UPDATE_CHECK'] = '1' if value else '0'
                    elif field == 'history_keep':
                        if type(value) is not int or value < 1:
                            raise RuleError('retention', '完成任务保留数量须为正整数。')
                        updates[KEYS[field]] = str(value)
                    else:
                        if field == 'binding':
                            if value is not None:
                                self.validate_binding(value, str(path))
                        elif not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
                            raise RuleError('models', '目标模型须为模型 ID 列表。')
                        mapping = json.loads(original.get(KEYS[field], '{}'))
                        if not isinstance(mapping, dict):
                            raise ValueError()
                        mapping[current['scope']] = value
                        updates[KEYS[field]] = json.dumps(mapping, ensure_ascii=False, separators=(',', ':'))
                    results[field] = {'status': 'saved'}
                except (RuleError, ValueError, TypeError) as error:
                    results[field] = {'status': 'failed', 'code': getattr(error, 'code', 'config_invalid'),
                        'message': getattr(error, 'message', f'目标文件中的本项 JSON 无法安全更新：{path}')}
            if updates:
                fresh = self.read(data.get('scope'), tolerate_errors=True)
                related = [key for key, outcome in results.items() if outcome['status'] == 'saved']
                if any(fresh['field_revisions'][key] != current['field_revisions'][key] for key in related):
                    raise RuleError('config_changed', '写入前相关配置已变化；草稿保留。')
                latest = path.read_text(encoding='utf-8-sig') if path.exists() else ''
                lines = [line for line in latest.splitlines() if line.partition('=')[0].strip() not in updates]
                lines.extend(f'{key}={value}' for key, value in updates.items())
                atomic_text(path, '\n'.join(lines) + '\n')
                actual = parse_env(path.read_text(encoding='utf-8-sig'))
                if any(actual.get(key) != value for key, value in updates.items()):
                    raise RuleError('config_uncertain', '写入后回读不一致，请保留草稿并重新核对。')
            result = self.read(data.get('scope'), tolerate_errors=True)
            result.update(saved_to=str(path), field_results=results,
                overridden=[key for key, outcome in results.items() if outcome['status'] == 'saved' and result['sources'][key] != str(path)])
            return result
