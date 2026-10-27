"""Observable filesystem/locking/portability regressions; no live business calls."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import _runtime_bootstrap as bootstrap
from pdf2markdown import cli, fsatomic, zipsafe
from .test_workflow_v2 import command, pdf_file, zip_bytes


def test_checkpoints_replace_existing_files_and_survive_a_failed_write(tmp_path, monkeypatch):
    path = str(tmp_path / 'state.json')
    fsatomic.atomic_replace_bytes(path, b'{"n":1}')
    fsatomic.atomic_replace_bytes(path, b'{"n":2}')
    real_replace = os.replace
    def fail(source, target):
        raise OSError('simulated disk error')
    with monkeypatch.context() as patch:
        patch.setattr(fsatomic.os, 'replace', fail)
        with pytest.raises(OSError):
            fsatomic.atomic_replace_bytes(path, b'{"n":3}')
    assert Path(path).read_bytes() == b'{"n":2}'
    assert not list(tmp_path.glob('.tmp-*'))
    with pytest.raises(fsatomic.UnsafePathError):
        fsatomic.atomic_write_bytes(path, b'overwrite')


def test_write_all_handles_partial_writes(tmp_path, monkeypatch):
    path = tmp_path / 'bytes'
    real_write = os.write
    monkeypatch.setattr(os, 'write', lambda fd, data: real_write(fd, data[:3]))
    fd = fsatomic.open_regular(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        fsatomic.write_all(fd, b'a\n\r\x1a' * 20)
    finally:
        os.close(fd)
    assert path.read_bytes() == b'a\n\r\x1a' * 20


def test_other_process_cannot_acquire_lock_until_owner_exits(tmp_path):
    work = tmp_path / 'job'
    (work / '.state').mkdir(parents=True)
    script = ('import sys; from pathlib import Path; from pdf2markdown.cli import locked; '
              '\nwith locked(Path(sys.argv[1])): pass')
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'scripts'))
    with cli.locked(work):
        blocked = subprocess.run([sys.executable, '-c', script, str(work)], env=env,
                                 capture_output=True, timeout=10)
        assert blocked.returncode != 0
        assert b'Another process' in blocked.stderr
    free = subprocess.run([sys.executable, '-c', script, str(work)], env=env,
                          capture_output=True, timeout=10)
    assert free.returncode == 0, free.stderr.decode()


def test_windows_lock_adapter_and_runtime_path(tmp_path, monkeypatch):
    # Adapter contract only; native Windows execution is a separate acceptance.
    calls = []
    fake = SimpleNamespace(LK_NBLCK=2, locking=lambda fd, mode, count: calls.append((mode, count)))
    fd = os.open(tmp_path / 'lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(fsatomic.os, 'name', 'nt')
            patch.setitem(sys.modules, 'msvcrt', fake)
            fsatomic.lock_exclusive(fd)
            assert calls == [(2, 1)]
            def conflict(*args):
                raise OSError(errno.EACCES, 'locked')
            fake.locking = conflict
            with pytest.raises(BlockingIOError):
                fsatomic.lock_exclusive(fd)
    finally:
        os.close(fd)
    assert bootstrap.venv_python('env', 'nt') == os.path.join('env', 'Scripts', 'python.exe')
    assert bootstrap.venv_python('env', 'posix') == os.path.join('env', 'bin', 'python')


@pytest.mark.parametrize('name', ['C:/outside.md', 'C:relative.md', 'CON.md', 'images/NUL.png',
                                   'a.md:stream', 'name. /x.md', '../directory/', 'a/../x.md'])
def test_zip_names_are_safe_on_both_platforms(tmp_path, name):
    source = tmp_path / 'bad.zip'
    source.write_bytes(zip_bytes('# good', {name: b'bad'}))
    with pytest.raises(zipsafe.ZipAdoptionError):
        zipsafe.adopt_result_zip(str(source), str(tmp_path / 'tree'))
    assert not (tmp_path / 'tree').exists()


def test_zip_file_directory_alias_is_rejected(tmp_path):
    source = tmp_path / 'bad.zip'
    source.write_bytes(zip_bytes('# good', {'Images': b'file', 'images/a.png': b'child'}))
    with pytest.raises(zipsafe.ZipAdoptionError, match='prefix conflict'):
        zipsafe.adopt_result_zip(str(source), str(tmp_path / 'tree'))


def test_real_entry_uses_project_output_setting_and_refuses_old_bundle(pdf_file, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / '.env.pdf2markdown').write_text('PDF2MARKDOWN_OUTPUT_DIR=chosen-folder\n')
    archive = tmp_path / 'existing.zip'
    archive.write_bytes(zip_bytes('# Text\n'))
    out = command(['run', '--source', str(pdf_file), '--result-zip', str(archive),
                   '--visual-capability', 'available'])
    assert out['outcome'] == 'host_action_required', out
    assert Path(out['work_bundle']).parent == tmp_path / 'chosen-folder'
    legacy = tmp_path / 'old-task'
    legacy.mkdir()
    (legacy / 'manifest.json').write_text('{"schema_version":1}')
    before = (legacy / 'manifest.json').read_bytes()
    out = command(['resume', '--work-bundle', str(legacy)])
    assert out['reason']['code'] == 'unsupported_bundle'
    assert 'v1.3.0' in out['reason']['message']
    assert (legacy / 'manifest.json').read_bytes() == before


def test_subprocess_entry_stdout_is_one_json_and_reopens_same_pending_action(pdf_file, tmp_path):
    from pdf2markdown import assets
    skill = Path(__file__).resolve().parents[1]
    archive = tmp_path / 'existing.zip'
    archive.write_bytes(zip_bytes('# Text\n'))
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=sys.prefix, PYTHONDONTWRITEBYTECODE='1')
    base = ['uv', 'run', '--frozen', '--project', str(skill), str(skill / 'scripts/workflow.py')]
    common = ['--pandoc', assets.find_pandoc()]
    initial = subprocess.run(base + ['run', '--source', str(pdf_file), '--result-zip', str(archive),
                              '--visual-capability', 'available'] + common,
                             env=env, cwd=tmp_path, capture_output=True, timeout=60)
    assert initial.returncode == 10, initial.stderr.decode()
    out = json.loads(initial.stdout)
    resumed = subprocess.run(base + ['resume', '--work-bundle', out['work_bundle']] + common,
                             env=env, cwd=tmp_path, capture_output=True, timeout=60)
    assert resumed.returncode == 10, resumed.stderr.decode()
    assert json.loads(resumed.stdout) == out
