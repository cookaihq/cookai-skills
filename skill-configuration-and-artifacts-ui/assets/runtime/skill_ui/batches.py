"""Independent configuration groups over an adapter's atomic save protocol.

The adapter still owns validation, locks, recovery and readback for each group.
Only fields with declared dependencies (or an atomic_group) share a commit.
"""
from copy import deepcopy
import logging
import hashlib
import json

from .contracts import UIError
from .scopes import validate_request
from .config_groups import submitted, dependencies, groups, check_baseline


def subset(request, ids):
    result = {name: {} for name in ('files', 'switches', 'file_edits')}
    for target, edits in request['files'].items():
        selected = {key: edit for key, edit in edits.items() if key in ids}
        if selected:
            result['files'][target] = selected
    result['switches'] = {key: intent for key, intent in request.get('switches', {}).items() if key in ids}
    result['file_edits'] = {target: [key for key in keys if key in ids]
                            for target, keys in request.get('file_edits', {}).items()}
    return result


def projection_token(snapshot, reads):
    """Hash opaque field revisions, never private field values."""
    return hashlib.sha256(json.dumps(sorted((field['id'], field.get('edit_revision'))
        for field in snapshot['fields'] if field['id'] in reads)).encode()).hexdigest()


def run(adapter, request, revision, *, saving=False):
    """Return per-field outcomes, preserving failed drafts and unrelated disk edits."""
    validate_request(request)
    initial = adapter.config()
    output = deepcopy(initial)
    output.update(validation_errors=[], scope_plans={}, unchanged_fields=[], field_results={}, preview_tokens={})
    results, errors = output['field_results'], []
    latest_revision = revision
    for members, reads in groups(initial, request):
        writing = False
        try:
            if members & set(request.get('invalid_fields', [])):
                raise UIError('validation', '请修正本项格式或核对已变化的关联设置；草稿已保留。', 422)
            current = adapter.config() if saving else initial
            check_baseline(current, request, latest_revision, reads)
            changes = subset(request, members)
            # Carry the checked dependency baseline through the adapter's lock.
            changes['field_revisions'] = {field['id']: field['edit_revision']
                for field in current['fields'] if field['id'] in reads}
            candidate = adapter.preview(changes, current['revision'])
            for name in ('scope_plans', 'directory_targets'):
                output.setdefault(name, {}).update(candidate.get(name, {}))
            if candidate.get('directory_revision'):
                output['directory_revision'] = candidate['directory_revision']
                # Each independent group has its own prospective directory state.
                # A previous group's commit must not invalidate unrelated groups.
                token = projection_token(candidate, reads)
                output['preview_tokens'][min(members)] = token
                if saving and request.get('preview_tokens', {}).get(min(members)) != token:
                    raise UIError('conflict', '请重新预览本项的新目录与保存目标，再保存修改。', 409)
                changes['directory_revision'] = candidate['directory_revision']
            if candidate.get('validation_errors'):
                raise UIError('validation', '；'.join(candidate['validation_errors']), 422)
            for key, plan in candidate.get('scope_plans', {}).items():
                if plan.get('cleanup') and changes.get('switches', {}).get(key, {}).get('cleanup_token') != plan.get('cleanup_token'):
                    raise UIError('confirmation', '请先确认保存后将清除的具体设置。', 422)
            output['unchanged_fields'].extend(candidate.get('unchanged_fields', []))
            if saving:
                writing = True
                candidate = adapter.save(changes, current['revision'])
                latest_revision = candidate['revision']
                output.update(candidate)
            else:
                # Keep independent projections together; failed fields retain the loaded state.
                projection = {field['id']: field for field in candidate['fields']}
                output['fields'] = [projection[field['id']] if field['id'] in reads else field
                                    for field in output['fields']]
                if any(field.get('directory_scope') and field['id'] in members for field in initial['fields']):
                    output['layers'] = candidate['layers']
            outcome = {'status': 'saved' if saving else 'ready'}
        except UIError as error:
            outcome = {'status': 'uncertain' if error.code == 'recovery' else 'failed',
                       'code': error.code, 'message': error.message}
        except Exception as error:
            # No exception text: loaders/OS errors may contain credentials.
            logging.error('config_group_failed type=%s fields=%s', type(error).__name__, ','.join(sorted(members)))
            outcome = {'status': 'uncertain' if writing else 'failed', 'code': 'internal',
                       'message': '保存结果需要回读核对。' if writing else '无法检查本项，请查看服务日志。'}
        for key in members:
            results[key] = dict(outcome)
        if outcome['status'] != ('saved' if saving else 'ready'):
            errors.append(outcome['message'])
        if outcome['status'] == 'uncertain':
            for key in submitted(request) - results.keys():
                results[key] = {'status': 'failed', 'code': 'not_attempted',
                                               'message': '等待前一组保存结果核对后再提交。'}
            break
    output['field_results'] = results
    output['validation_errors'] = list(dict.fromkeys(errors))
    output['save_status'] = ('uncertain' if any(r['status'] == 'uncertain' for r in output['field_results'].values())
                            else ('partial' if any(r['status'] in ('ready', 'saved') for r in results.values()) else 'failed')
                            if any(r['status'] == 'failed' for r in output['field_results'].values())
                            else 'saved' if saving else 'ready')
    return output
