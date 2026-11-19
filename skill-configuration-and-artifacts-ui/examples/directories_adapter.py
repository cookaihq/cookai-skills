"""Synthetic two-stage JSON loader. All roots must stay in this demo project.

Business adapters choose their own defaults, keys and source retirement policy.
This example keeps selectors in the default config.json files, replaces roots,
and includes global settings.json. It never reads real home or process values.
"""
from copy import deepcopy
import hashlib
import hmac
import json
from pathlib import Path
import secrets

from skill_ui import Adapter, UIError
from skill_ui.config_groups import groups, check_request_baseline
from skill_ui.directories import retarget_request, selector_edits, check_directory_revision, planning_snapshot
from skill_ui.scopes import ScopePlanner, annotate_snapshot, validate_request
from skill_ui.storage import apply_changes, file_lock
from skill_ui.transactions import JsonTransaction


class DirectoryDemo(Adapter):
    title = '配置目录 · 合成示例'

    def __init__(self, project):
        self.project = Path(project).resolve()
        self.directory = self.project / 'demo-directories'
        self.defaults = {scope: self.directory / scope for scope in ('global', 'project')}
        self.anchors = {scope: root / 'config.json' for scope, root in self.defaults.items()}
        self.secret = secrets.token_bytes(32)
        self.environment = {}

    @staticmethod
    def layer_id(path):
        return hashlib.sha256(str(path).encode()).hexdigest()[:20]

    def _checked_demo_path(self, path):
        path = Path(path)
        resolved = path.resolve()
        if not path.is_absolute() or not resolved.is_relative_to(self.directory):
            raise UIError('target', '演示目录必须是 demo-directories 内的完整绝对路径。', 422)
        for component in (path, *path.parents):
            if component == self.project:
                break
            if component.is_symlink() or getattr(component, 'is_junction', lambda: False)():
                raise UIError('target', '合成演示不读取链接文件或链接目录。', 422)
        return resolved

    def _state(self, overrides=None):
        overrides = overrides or {}
        data, errors = {}, {}

        def read(path):
            name = str(path)
            if name in data:
                return data[name]
            try:
                checked = self._checked_demo_path(path)
                value = overrides[name] if name in overrides else json.loads(checked.read_text(encoding='utf-8'))
                if not isinstance(value, dict):
                    raise ValueError()
                data[name] = deepcopy(value)
            except FileNotFoundError:
                data[name] = {}
            except (ValueError, OSError, UnicodeError, UIError):
                data[name], errors[name] = {}, '配置文件无法读取或格式无效。'
            return data[name]

        roots, selector_sources = {}, {}
        for scope, anchor in self.anchors.items():
            key = scope + '_directory'
            value = self.environment.get(key) or read(anchor).get(key)
            source = 'process' if self.environment.get(key) else self.layer_id(anchor) if value else 'default'
            selector_sources[key] = source
            root = Path(value) if isinstance(value, str) and value.strip() else self.defaults[scope]
            try:
                root = self._checked_demo_path(root)
            except (UIError, OSError, ValueError):
                errors[str(anchor)] = '演示目录必须是 demo-directories 内的完整绝对路径。'
                root = self.defaults[scope]
            roots[scope] = root
        paths = {scope: root / 'config.json' for scope, root in roots.items()}
        paths['settings'] = roots['global'] / 'settings.json'
        for path in paths.values():
            read(path)
        layers = [dict(id='process', name='演示进程环境', scope='process', path='演示进程', readonly=True)]
        for scope, path in [('project', paths['project']), ('global', paths['global']),
                            ('global', paths['settings']), *[(s, p) for s, p in self.anchors.items()]]:
            if any(layer['id'] == self.layer_id(path) for layer in layers):
                continue
            layers.append(dict(id=self.layer_id(path), name=path.name, scope=scope, path=str(path),
                               scope_label='仅合成演示，不读取真实配置。', readonly=False, exists=path.exists()))
        definitions = [dict(id=scope + '_directory', label=('全局' if scope == 'global' else '当前项目') + '配置目录',
                            type='text', directory_scope=scope, default=str(self.defaults[scope]), anchor=str(anchor))
                       for scope, anchor in self.anchors.items()]
        definitions += [dict(id='api_key', label='合成 API Key', type='text', sensitive=True),
                        dict(id='model', label='模型', type='text', default='demo-model'),
                        dict(id='favorite', label='收藏', type='text', default='')]
        fields = []
        for definition in definitions:
            key, scope = definition['id'], definition.get('directory_scope')
            allowed = [str(self.anchors[scope])] if scope else [str(paths['settings'])] if key == 'favorite' else [str(paths[s]) for s in ('project', 'global')]
            candidates = ['process', *allowed]
            winner, value = 'default', definition.get('default')
            for name in candidates:
                own = self.environment if name == 'process' else data[name]
                if key in own and own[key] not in (None, ''):
                    winner, value = ('process' if name == 'process' else self.layer_id(name)), own[key]
                    break
            if scope:
                winner, value = selector_sources[key], str(roots[scope])
            rows = []
            for layer in layers:
                name = layer['path']
                own = self.environment if layer['id'] == 'process' else data.get(name, {})
                applies = layer['id'] == 'process' or name in allowed
                present = applies and key in own
                rows.append(dict(layer=layer['id'], value=own.get(key) if applies else None, present=present,
                    status='skipped' if not applies else 'error' if name in errors else 'missing' if not present else
                    'empty' if own[key] in (None, '') else 'effective' if layer['id'] == winner else 'shadowed'))
            fields.append(dict(definition, config_key='$.' + key, targets=[self.layer_id(p) for p in allowed],
                rows=rows, effective=dict(source=winner, value=value, status='set' if winner != 'default' else 'default')))
        revision = hmac.new(self.secret, json.dumps([data, errors, self.environment], sort_keys=True).encode(), hashlib.sha256).hexdigest()
        return annotate_snapshot(dict(revision=revision, fields=fields, layers=layers,
            directories={scope: str(root) for scope, root in roots.items()},
            default_targets={scope: self.layer_id(paths[scope]) for scope in roots},
            validation_errors=list(set(errors.values())), presentation={'focus': ['api_key', 'model', 'favorite']}), self.secret), data

    def config(self):
        journal = self._checked_demo_path(self.directory / 'transaction.json')
        if journal.exists():
            # Recovery is confined to the example's known JSON filenames inside
            # its synthetic tree, including a root half-written by an old save.
            allowed = [self._checked_demo_path(p) for name in ('config.json', 'settings.json') for p in self.directory.rglob(name)]
            with file_lock(self._checked_demo_path(self.directory / 'save.lock'), protect_git=True, owner_path=self.project):
                JsonTransaction(journal, allowed, self.project).recover()
        return self._state()[0]

    def _edits(self, snapshot, data, files):
        error_layers = {row['layer'] for field in snapshot['fields'] for row in field['rows'] if row['status'] == 'error'}
        error_paths = {layer['path'] for layer in snapshot['layers'] if layer['id'] in error_layers}
        # An unreadable file is never an empty override in the next projection.
        values = {name: deepcopy(value) for name, value in data.items() if name not in error_paths}
        for target, changes in files.items():
            layer = next((l for l in snapshot['layers'] if l['id'] == target), None)
            if not layer or layer['readonly']:
                raise UIError('target', '请选择可写文件。', 422)
            if layer['path'] in error_paths:
                raise UIError('target', '配置文件无法安全更新：' + layer['path'], 422)
            fields = [f for f in snapshot['fields'] if target in f['targets']]
            values[layer['path']] = apply_changes(values[layer['path']], fields, changes)
        return values

    def _candidate(self, request, revision, *, saving=False):
        validate_request(request)
        current, data = self._state()
        check_request_baseline(current, request, revision)
        selectors = selector_edits(current, request)
        anchors = self._edits(current, data, selectors)
        prospective, projected = self._state(anchors)
        reads = set().union(*(related for _, related in groups(current, request)))
        failed = {row['layer'] for field in prospective['fields'] if field['id'] in reads
                  for row in field['rows'] if row['status'] == 'error'}
        if failed:
            paths = [layer['path'] for layer in prospective['layers'] if layer['id'] in failed]
            message = '关联配置文件无法读取或格式无效：' + '、'.join(paths)
            if saving:
                raise UIError('target', message, 422)
            # Preview reports the real source errors without generating a write
            # candidate. Save repeats the check under the transaction lock.
            prospective.update(validation_errors=[message], scope_plans={}, unchanged_fields=[])
            return prospective, {}
        baseline_revision = prospective['revision']
        if selectors:
            check_directory_revision(request, baseline_revision, saving=saving)

        def destination(field, layer):
            root = current['directories'][layer['scope']]
            path = Path(prospective['directories'][layer['scope']]) / Path(layer['path']).relative_to(root)
            return self.layer_id(path)

        normalized, unchanged = retarget_request(current, prospective, request, destination)
        prospective['revision'] = baseline_revision
        def project(files):
            values = self._edits(prospective, projected, files)
            candidate, _ = self._state(values)
            affected = {key for edits in files.values() for key in edits}
            affected.update(key for edits in selectors.values() for key in edits)
            candidate['validation_errors'] = list({
                '配置文件无法读取或格式无效。' for field in candidate['fields'] if field['id'] in affected
                for row in field['rows'] if row['status'] == 'error'})
            touched = {next(l['path'] for l in prospective['layers'] if l['id'] == target) for target in files}
            touched.update(str(self.anchors[f['directory_scope']]) for f in current['fields']
                           if f.get('directory_scope') and any(f['id'] in edits for edits in request['files'].values()))
            candidate['directory_targets'] = {key: next(l['path'] for l in prospective['layers'] if l['id'] == target)
                                              for target, changes in files.items() for key in changes}
            return candidate, {Path(name): values[name] for name in touched}
        candidate, updates = ScopePlanner(planning_snapshot(current, prospective), self.secret, project).plan(normalized, baseline_revision)
        candidate['unchanged_fields'].extend(unchanged)
        if selectors:
            candidate['directory_revision'] = baseline_revision
        return candidate, updates

    def preview(self, changes, revision):
        self.config()
        return self._candidate(changes, revision)[0]

    def save(self, changes, revision):
        self.config()
        with file_lock(self._checked_demo_path(self.directory / 'save.lock'), protect_git=True, owner_path=self.project):
            candidate, updates = self._candidate(changes, revision, saving=True)
            if candidate['validation_errors']:
                raise UIError('validation', '；'.join(candidate['validation_errors']), 422)
            transaction = JsonTransaction(self.directory / 'transaction.json', updates, self.project)
            def check():
                fresh, _ = self._candidate(changes, revision, saving=True)
                if fresh['revision'] != candidate['revision']:
                    raise UIError('conflict', '保存前来源已变化，请重新预览核对。', 409)
            def verify():
                actual = self._state()[0]
                if actual['revision'] != candidate['revision']:
                    raise UIError('verification', '保存后来源核对失败。', 409)
                return actual
            return transaction.commit(updates, verify, check=check)


def build(project):
    return DirectoryDemo(project)
