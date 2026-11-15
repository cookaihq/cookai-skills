"""Opt-in JSON transaction for examples/new stores, not a replacement business loader.

Call under the store's lock. A private journal makes interrupted writes recoverable;
it does not provide isolation from arbitrary external readers/writers.
"""
import base64
import json
import os
from pathlib import Path
import secrets

from .contracts import UIError
from .storage import (_checked_path, _private_parents, _private_windows, atomic_json,
                      prepare_private_write)


class JsonTransaction:
    def __init__(self, journal, allowed, owner_path):
        self.journal = Path(journal)
        self.allowed = {str(Path(p).absolute()) for p in allowed}
        self.owner_path = owner_path

    @staticmethod
    def read(path):
        path = _checked_path(path)
        return path.read_bytes() if path.exists() else None

    def write(self, path, raw):
        prepare_private_write(path, owner_path=self.owner_path)
        if raw is None:
            if path.exists():
                path.unlink()
            return
        _private_parents(path, self.owner_path)
        temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
        prepare_private_write(temporary, temporary_for=path, owner_path=self.owner_path)
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
                if os.name == 'nt':
                    _private_windows(temporary, self.owner_path)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            prepare_private_write(path, owner_path=self.owner_path)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def recover(self):
        if not self.journal.exists():
            return
        try:
            record = json.loads(self.read(self.journal))
            if record['state'] not in ('applying', 'committed') or not isinstance(record['entries'], list):
                raise ValueError()
            entries = []
            for item in record['entries']:
                if item['path'] not in self.allowed:
                    raise ValueError()
                entries.append((Path(item['path']), *[
                    None if item[k] is None else base64.b64decode(item[k], validate=True)
                    for k in ('before', 'after')]))
            if record['state'] == 'applying':
                # Check every file before restoring any, so external changes survive.
                if any(self.read(path) not in (before, after) for path, before, after in entries):
                    raise UIError('recovery', '上次保存未完成且文件已被外部修改，请先修复；不会覆盖新内容。', 409)
                for path, before, after in reversed(entries):
                    if self.read(path) != before:
                        self.write(path, before)
            self.journal.unlink()
        except UIError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise UIError('recovery', '上次保存的恢复记录需要检查；已停止后续保存。', 409) from None

    def commit(self, updates, verify):
        if self.journal.exists():
            raise UIError('recovery', '请先回读并恢复上次保存。', 409)
        entries = []
        for path, data in updates.items():
            path = Path(path).absolute()
            if str(path) not in self.allowed:
                raise UIError('target', '保存文件不在允许范围内。', 422)
            prepare_private_write(path, owner_path=self.owner_path)
            after = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
            before = self.read(path)
            if before != after:
                entries.append((path, before, after))
        if not entries:
            return verify()
        record = {'state': 'applying', 'entries': [dict(path=str(path), **{
            key: None if raw is None else base64.b64encode(raw).decode('ascii')
            for key, raw in [('before', before), ('after', after)]}) for path, before, after in entries]}
        atomic_json(self.journal, record, owner_path=self.owner_path, protect_git=True)
        try:
            for path, before, after in entries:
                if self.read(path) != before:
                    raise UIError('conflict', '配置已被其他操作修改，请重新载入核对。', 409)
                self.write(path, after)
            result = verify()
            record['state'] = 'committed'
            atomic_json(self.journal, record, owner_path=self.owner_path, protect_git=True)
        except Exception:
            self.recover()
            raise
        # After the durable commit marker, a lost response must be resolved by reread.
        self.journal.unlink()
        return result
