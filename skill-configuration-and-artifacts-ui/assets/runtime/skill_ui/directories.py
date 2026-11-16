"""Project explicit edits after a business adapter resolves its new roots.

No product paths, selector keys, file formats or fallback policies live here.
The adapter stages fixed selector files, resolves with its real loader, then
supplies destination(field, old_layer) and commits through its own transaction.
"""
from copy import deepcopy

from .contracts import UIError
from .scopes import validate_request


def selector_edits(snapshot, request):
    validate_request(request)
    fields = {field['id']: field for field in snapshot['fields']}
    return {target: selected for target, changes in request['files'].items()
            if (selected := {key: change for key, change in changes.items()
                             if fields.get(key, {}).get('directory_scope')})}


def check_directory_revision(request, revision, *, saving=False):
    """Bind an approved preview to every source consulted in the candidate roots."""
    supplied = request.get('directory_revision')
    if saving and supplied is None:
        raise UIError('conflict', '请先预览配置目录和保存目标，再保存本次修改。', 409)
    if supplied is not None and supplied != revision:
        raise UIError('conflict', '新配置目录或其中的文件已变化，草稿已保留；请重新预览核对。', 409)


def planning_snapshot(current, prospective):
    """Keep original user inputs while planning against candidate files/targets.

    A directory switch changes the saved sources, not the content the user
    explicitly asked to transfer. The final projection still uses real roots.
    """
    snapshot = deepcopy(prospective)
    baselines = {field['id']: field['effective'] for field in current['fields']}
    for field in snapshot['fields']:
        if field['id'] in baselines:
            field['effective'] = deepcopy(baselines[field['id']])
    return snapshot


def retarget_request(current, prospective, request, destination):
    """Return a checked envelope and unchanged IDs, retaining original intent.

    A business edit equal to the original baseline is omitted. An edit equal to
    the *new* root's value still counts as deliberate. Fixed selector targets
    and explicit file actions keep their meaning; root selection never copies
    the rest of the configuration.
    """
    validate_request(request)
    fields = {field['id']: field for field in current['fields']}
    layers = {layer['id']: layer for layer in current['layers']}
    new_fields = {field['id']: field for field in prospective['fields']}
    files, explicit, unchanged = {}, {}, []

    def target_for(target, key):
        field, layer = fields.get(key), layers.get(target)
        if (not field or not layer or field.get('readonly') or layer['readonly'] or
                layer.get('enabled') is False or target not in field['targets']):
            raise UIError('target', '此字段不能保存到所选位置。', 422)
        new_target = target if field.get('directory_scope') else destination(field, layer)
        if new_target not in new_fields[key]['targets']:
            raise UIError('target', '新目录不允许保存所选字段，请选择可写的配置文件。', 422)
        return new_target

    for target, changes in request['files'].items():
        for key, change in changes.items():
            new_target = target_for(target, key)
            baseline = fields[key]['effective']['value']
            if (key not in request.get('file_edits', {}).get(target, []) and change['op'] == 'set' and
                    type(change['value']) is type(baseline) and change['value'] == baseline):
                unchanged.append(key)
                continue
            if key in files.setdefault(new_target, {}):
                raise UIError('target', '同一字段不能重复指定到新目录的同一文件。', 422)
            files[new_target][key] = deepcopy(change)
            # Prevent the ordinary planner comparing against the new baseline.
            explicit.setdefault(new_target, []).append(key)
    switches = deepcopy(request.get('switches', {}))
    for key, intent in switches.items():
        if fields.get(key, {}).get('directory_scope'):
            raise UIError('target', '目录设置保存在固定位置，不能切换范围。', 422)
        intent['target'] = target_for(intent.get('target'), key)
        # Keep actual explicit deletion/empty operations blocked by ScopePlanner.
        if not any(key in keys for keys in request.get('file_edits', {}).values()):
            for keys in explicit.values():
                if key in keys:
                    keys.remove(key)
    return dict(files=files, switches=switches, file_edits=explicit), unchanged
