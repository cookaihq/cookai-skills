"""Submitted dependency groups and revision checks, shared by planning and commits."""
from .contracts import UIError


def submitted(request):
    return set(request.get('switches', {})) | {key for edits in request['files'].values() for key in edits}


def dependencies(snapshot):
    fields = {field['id']: field for field in snapshot['fields']}
    graph = {key: {key} for key in fields}
    for key, field in fields.items():
        related = set(field.get('depends_on', {})) | set(field.get('dependencies', []))
        if field.get('atomic_group'):
            related.update(other for other, definition in fields.items()
                           if definition.get('atomic_group') == field['atomic_group'])
        for other in related & fields.keys():
            graph[key].add(other)
            graph[other].add(key)
    return graph


def groups(snapshot, request):
    graph = dependencies(snapshot)
    ids = submitted(request)
    if ids - graph.keys():
        raise UIError('changes', '变更包含未知字段。', 422)
    # A root selector and edits whose destinations depend on it are one transaction.
    for field in snapshot['fields']:
        key, scope = field['id'], field.get('directory_scope')
        if key not in ids or not scope:
            continue
        layers = {layer['id']: layer for layer in snapshot['layers']}
        for definition in snapshot['fields']:
            other = definition['id']
            if other == key or definition.get('directory_scope'):
                continue
            targets = [target for target, edits in request['files'].items() if other in edits]
            if other in request.get('switches', {}):
                targets.append(request['switches'][other].get('target'))
            # Changing a root also changes the effective values of unedited items.
            targets.extend(definition.get('targets', []))
            if any(layers.get(target, {}).get('scope') == scope for target in targets):
                graph[key].add(other)
                graph[other].add(key)
    remaining, result = set(ids), []
    while remaining:
        reached, pending = set(), {min(remaining)}
        while pending:
            key = pending.pop()
            reached.add(key)
            pending.update(graph[key] - reached)
        members = reached & ids
        result.append((members, reached))
        remaining -= members
    return result


def check_baseline(snapshot, request, revision, reads):
    expected = request.get('field_revisions')
    if expected is None:
        if snapshot['revision'] != revision:
            raise UIError('conflict', '配置已变化。草稿仍保留，请重新载入后核对。', 409)
        return
    fields = {field['id']: field for field in snapshot['fields']}
    if any(not expected.get(key) or expected[key] != fields[key].get('edit_revision') for key in reads):
        raise UIError('conflict', '本项或关联设置已变化。草稿仍保留，请重新载入后核对。', 409)


def check_request_baseline(snapshot, request, revision):
    if 'field_revisions' not in request:
        check_baseline(snapshot, request, revision, set())
    else:
        for _, reads in groups(snapshot, request):
            check_baseline(snapshot, request, revision, reads)


