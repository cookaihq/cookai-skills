"""Field-scoped plans; the business adapter owns projection and atomic persistence."""
from copy import deepcopy
import hashlib
import hmac
import json

from .contracts import UIError

PROTOCOL = 'field-scopes-v1'


def validate_request(request):
    if not isinstance(request, dict) or 'files' not in request or set(request) - {'files', 'switches', 'file_edits'}:
        raise UIError('contract', '配置请求需要迁移到逐项范围协议；请同时更新页面和适配器。', 422)


def annotate_snapshot(snapshot, secret, empty=None):
    """Attach opaque edit fingerprints; empty semantics may be supplied by the loader."""
    snapshot['configuration_protocol'] = PROTOCOL
    for field in snapshot['fields']:
        effective = field['effective']
        value = effective.get('value')
        effective['empty'] = bool(empty(field, value) if empty else value is None or isinstance(value, str) and not value.strip())
        field['edit_revision'] = hmac.new(secret, json.dumps(
            [field['rows'], effective, field['targets']], sort_keys=True, ensure_ascii=False,
        ).encode(), hashlib.sha256).hexdigest()
    return snapshot


class ScopePlanner:
    def __init__(self, snapshot, secret, project):
        self.current, self.secret, self.project = snapshot, secret, project

    @staticmethod
    def _same(left, right):
        return type(left) is type(right) and left == right

    def plan(self, request, revision):
        """Plan a field switch against private values, then reuse the transaction.

        Viewing a block never sends a switch. An explicit attempt can return an
        unresolved choice; both save entry points must submit that same choice.
        Tokens bind a choice to this input, target and loader revision without
        sending saved secrets to the browser for comparisons.
        """
        validate_request(request)
        current = self.current
        if current['revision'] != revision:
            raise UIError('conflict', '配置已变化。草稿仍保留，请重新载入后核对。', 409)
        files = deepcopy(request.get('files', {}))
        switches = request.get('switches', {})
        explicit = request.get('file_edits', {})
        if not all(isinstance(value, dict) for value in (files, switches, explicit)) or any(
                not isinstance(changes, dict) for changes in files.values()):
            raise UIError('changes', '变更格式无效。', 422)
        fields = {f['id']: f for f in current['fields']}
        layers = {l['id']: l for l in current['layers']}
        plans, errors, unchanged = {}, [], []
        for target, changes in files.items():
            layer = layers.get(target)
            if not layer or layer.get('readonly') or layer.get('enabled') is False:
                raise UIError('target', '所选配置位置不可写。', 422)
            for key, change in changes.items():
                field = fields.get(key)
                if not field or field.get('readonly') or target not in field['targets']:
                    raise UIError('target', '此字段不能保存到所选位置。', 422)
                if not isinstance(change, dict) or change.get('op') not in ('set', 'unset') or change.get('op') == 'set' and 'value' not in change:
                    raise UIError('changes', '变更格式无效。', 422)
        for target, keys in explicit.items():
            if not isinstance(keys, list) or any(not isinstance(key, str) or key not in files.get(target, {}) for key in keys):
                raise UIError('changes', '文件操作与本次提交不一致。', 422)
        # Drop normal edits returned to their loaded baseline, including secrets.
        # Explicit file removals / empty values retain their file-specific meaning.
        for target, changes in files.items():
            for key, change in list(changes.items()):
                field = fields.get(key)
                if field and isinstance(change, dict) and change.get('op') == 'set' and key not in explicit.get(target, []) and self._same(change.get('value'), field['effective']['value']):
                    del changes[key]
                    unchanged.append(key)
        for key, intent in switches.items():
            field = fields.get(key)
            if not field or not isinstance(intent, dict):
                raise UIError('changes', '切换设置无效。', 422)
            target = intent.get('target')
            layer = layers.get(target)
            if not layer or target not in field['targets'] or layer['readonly'] or layer.get('enabled') is False or field.get('readonly'):
                raise UIError('readonly', field['label'] + '不能使用这个位置。', 422)
            rows = {r['layer']: r for r in field['rows']}
            row = rows[target]
            plan = plans[key] = dict(target=target, state='ready', cleanup=[], writes_target=False)
            edits = [changes[key] for changes in files.values() if key in changes]
            if any(key in ids for ids in explicit.values()) or any(edit.get('op') != 'set' for edit in edits) or len(edits) > 1:
                plan.update(state='blocked', message='请先保存或撤销本项的文件删除／清空操作，再切换位置。')
                errors.append(plan['message'])
                continue
            entered = edits[0]['value'] if edits else field['effective']['value']
            dirty = not self._same(entered, field['effective']['value'])
            has_target = row['status'] in ('effective', 'shadowed') and row['present']
            same_source = field['effective']['source'] == target
            mode = intent.get('mode', 'existing')
            if mode not in ('existing', 'input'):
                raise UIError('changes', '请选择有效的切换方式。', 422)
            token = hmac.new(self.secret, json.dumps([revision, key, target, entered, mode], ensure_ascii=False, sort_keys=True).encode(), hashlib.sha256).hexdigest()
            conflict = not same_source and has_target and dirty and not self._same(entered, row['value'])
            choice = intent.get('choice') if intent.get('choice_token') == token else None
            plan.update(conflict=conflict, choice_token=token, input_changed=dirty)
            if conflict and choice not in ('input', 'target'):
                plan.update(state='choice', message='这里已有不同内容，请选择本项要使用的内容。')
                errors.append(field['label'] + '：请选择要使用的内容。')
                continue
            value = (row['value'] if has_target and not same_source and
                     (not dirty and mode == 'existing' or choice == 'target') else entered)
            plan['uses_target'] = has_target and self._same(value, row['value'])
            # All participating higher sources matter, including files hidden by
            # another file in the same scope. Skipped sources never participate.
            higher = []
            for upper in current['layers']:
                if upper['id'] == target:
                    break
                upper_row = rows.get(upper['id'], {})
                if upper_row.get('status') == 'error':
                    errors.append(field['label'] + '的更高位置无法读取，请修复后重新载入。')
                    plan.update(state='blocked', message=errors[-1])
                if upper_row.get('present') and upper_row.get('status') in ('effective', 'shadowed'):
                    if upper['readonly'] or upper['id'] not in field['targets']:
                        errors.append(field['label'] + '被只读的进程环境或文件覆盖，无法在页面切换。')
                        plan.update(state='blocked', message=errors[-1])
                    else:
                        higher.append(upper['id'])
            if row['status'] == 'error':
                errors.append(field['label'] + '的目标文件无法读取，请修复后重新载入。')
                plan.update(state='blocked', message=errors[-1])
            if plan['state'] == 'blocked':
                continue
            for changes in files.values():
                changes.pop(key, None)
            if not has_target or not self._same(value, row['value']):
                files.setdefault(target, {})[key] = dict(op='set', value=value)
                plan['writes_target'] = True
            for upper in higher:
                files.setdefault(upper, {})[key] = dict(op='unset')
            plan['cleanup'] = higher
            plan['message'] = ('使用这里已有的内容。' if plan['uses_target'] else '保存当前填写的内容并使用。')
            if higher:
                plan['message'] += ' 同时移除 ' + str(len(higher)) + ' 处会覆盖它的本项设置；更低位置保留。'
            else:
                plan['message'] += ' 其他位置已有的设置保留。'
        files = {target: changes for target, changes in files.items() if changes}
        candidate, modifications = self.project(files) if files else (deepcopy(current), {})
        for key, plan in plans.items():
            if plan['state'] != 'ready':
                continue
            after = next(f for f in candidate['fields'] if f['id'] == key)
            if after['effective']['source'] != plan['target']:
                plan.update(state='blocked', message='保存后仍无法使用所选位置，请检查覆盖来源。')
                errors.append(plan['message'])
        candidate['validation_errors'] = sorted(set(candidate['validation_errors'] + errors))
        candidate['scope_plans'] = plans
        candidate['unchanged_fields'] = unchanged
        return candidate, modifications
