import hashlib
import io
import json
import os
from pathlib import Path
import socket
import zipfile

import fitz
from lxml import html
import pytest

import aihub_upload
import pdf_source
from pdf2markdown import assets, cli, doc2x, jcs, network


@pytest.fixture
def pdf_file(tmp_path):
    path = tmp_path / 'sample.pdf'
    doc = fitz.open()
    for title in ('Voltage: 5 V', 'Second page, all content retained'):
        page = doc.new_page(width=300, height=400)
        page.insert_text((30, 50), title)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def png():
    pix = fitz.Pixmap(fitz.csRGB, (0, 0, 8, 8), False)
    pix.clear_with(180)
    return pix.tobytes('png')


def zip_bytes(markdown, members=None, name='output.md'):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr(name, markdown)
        for key, value in (members or {}).items():
            z.writestr(key, value)
    return out.getvalue()


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self.status = status
        self.headers = headers or [('Content-Length', str(len(body)))]
        self.body = io.BytesIO(body)
    def read(self, size, *, timeout):
        assert timeout > 0
        return self.body.read(size)
    def close(self):
        pass


class FakeDownload:
    """Exercise the real DNS/peer/download code with a scripted HTTPS socket."""
    def __init__(self, responses, address='93.184.216.34'):
        self.responses = iter(responses)
        self.calls = []
        self.address = address
    def resolve(self, host, port):
        family = socket.AF_INET6 if ":" in self.address else socket.AF_INET
        address = (self.address, port, 0, 0) if family == socket.AF_INET6 else (self.address, port)
        return [pdf_source.ResolvedEndpoint(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, address, self.address)]
    def connect_https(self, host, port, **kwargs):
        assert kwargs['proxies'] is False
        self.peer_ip = self.address
        return self
    def get(self, target, **kwargs):
        self.calls.append((target, kwargs))
        assert all(k.lower() not in {'authorization', 'cookie', 'referer'} for k in kwargs['headers'])
        return next(self.responses)
    def close(self):
        pass


def command(argv, services=None):
    return cli.execute(cli.parser().parse_args(argv), services=services)


def review_payload(action, *, repair=False, findings=None):
    items = []
    for block in action['blocks']:
        altered = repair and 'Volatge' in block['markdown_fragment']
        items.append({'block_id': block['block_id'], 'verdict': 'altered' if altered else 'match',
                      'evidence_page': action['page_number'] if altered else None,
                      'evidence_bbox': [0, 0, 1, 1] if altered else None,
                      'replacement_text': block['markdown_fragment'].replace('Volatge', 'Voltage') if altered else None})
    return {'page_complete': True, 'reviews': items, 'page_findings': findings or []}


def finish(out, tmp_path, *, services=None, repair=False):
    pages = []
    while out['outcome'] == 'host_action_required':
        assert len(pages) < 6
        action = out['action']
        pages.append(action['page_number'])
        response = tmp_path / 'review-response.json'
        response.write_text(json.dumps(review_payload(action, repair=repair)))
        out = command(['record-review', '--work-bundle', out['work_bundle'],
                       '--action-id', action['action_id'], '--input', str(response)], services)
    assert out['outcome'] == 'completed', out
    return out, pages


def test_normal_run_resumes_same_paid_task_and_exports_corrected_markdown(pdf_file, png, tmp_path, monkeypatch):
    monkeypatch.setenv('AIHUB_API_KEY', 'test-only-key')
    body = zip_bytes('# Volatge: 5 V\n\n![diagram](https://cdn.example/image.png "Diagram")\n\n'
                     'Second page, all content retained.\n', name='chapter/direct.md')
    downloads = FakeDownload([FakeResponse(body)])
    uploads = []
    tasks = []
    def upload(method, url, headers, stream):
        data = b''.join(stream)
        assert pdf_file.read_bytes() in data
        uploads.append(url)
        return aihub_upload.Response(200, b'{"url":"https://stage.example/source.pdf?secret=hidden"}')
    def task(request):
        tasks.append(request)
        response = {'id': 'task-1', 'model': 'doc2x-v3', 'type': 'document', 'status': 'completed'}
        if request['method'] == 'GET':
            response['results'] = [{'url': 'https://result.example/result.zip?secret=hidden'}]
        return 200, json.dumps(response).encode()
    services = {'upload_transport': upload, 'doc2x_transport': task,
                'download_transport': downloads, 'asset_fetch': lambda url, limit: png}
    out = command(['run', '--source', str(pdf_file), '--output-dir', str(tmp_path / 'output'),
                   '--visual-capability', 'available'], services)
    assert out['outcome'] == 'host_action_required', out
    again = command(['resume', '--work-bundle', out['work_bundle']], services)
    assert out == again
    assert 'secret=hidden' not in json.dumps(out)
    out, pages = finish(out, tmp_path, services=services, repair=True)
    assert len(pages) == 3  # every page once, modified page twice
    assert len(uploads) == 1
    assert [r['method'] for r in tasks] == ['POST', 'GET']
    assert tasks[0]['body']['pdf_url'].startswith('https://stage.example/')
    paths = out['artifacts']
    assert '# Voltage: 5 V' in Path(paths['final.md']).read_text()
    assert '# Volatge: 5 V' in Path(paths['direct.md']).read_text()
    assert Path(paths['source.pdf']).read_bytes() == pdf_file.read_bytes()
    final = Path(paths['final.md'])
    targets = assets.discover_image_targets(final.read_bytes(), assets.find_pandoc())
    assert targets and all((final.parent / name).is_file() for name in targets)
    tree = html.fromstring(Path(paths['index.html']).read_bytes())
    assert len(tree.xpath('//object[@data="source.pdf"]')) == 1
    assert {a.get('href') for a in tree.xpath('//a[@download]')} == {'source.pdf', 'direct.md', 'final.md'}
    assert all(i.get('src') in targets for i in tree.xpath('//img'))
    # A completed resume validates and returns the same files without API calls.
    assert command(['resume', '--work-bundle', out['work_bundle']], services) == out
    assert len(tasks) == 2


def test_local_zip_flow_requires_no_api_key_and_checks_every_page(pdf_file, tmp_path, monkeypatch):
    monkeypatch.delenv('AIHUB_API_KEY', raising=False)
    z = tmp_path / 'existing.zip'
    z.write_bytes(zip_bytes('# First\n\nSecond page content.\n'))
    out = command(['run', '--source', str(pdf_file), '--result-zip', str(z),
                   '--output-dir', str(tmp_path / 'out'), '--visual-capability', 'available'])
    out, pages = finish(out, tmp_path)
    assert sorted(pages) == [1, 2]
    assert out['quality'] == 'reviewed'


def test_unknown_submission_never_replays_post(pdf_file, tmp_path, monkeypatch):
    monkeypatch.setenv('AIHUB_API_KEY', 'test-only-key')
    calls = []
    def upload(method, url, headers, stream):
        b''.join(stream)
        return aihub_upload.Response(200, b'{"url":"https://stage.example/source.pdf"}')
    def task(request):
        calls.append(request)
        raise doc2x.Doc2XReason('submission_unknown', 'connection interrupted')
    services = {'upload_transport': upload, 'doc2x_transport': task}
    out = command(['run', '--source', str(pdf_file), '--output-dir', str(tmp_path / 'out'),
                   '--visual-capability', 'available'], services)
    assert out['outcome'] == 'failed'
    out = command(['resume', '--work-bundle', out['work_bundle']], services)
    assert out['reason']['raw_reason'] == 'submission_unknown'
    assert len(calls) == 1


def test_changed_credential_blocks_task_after_interruption(pdf_file, tmp_path, monkeypatch):
    monkeypatch.setenv('AIHUB_API_KEY', 'first-test-key')
    def upload(method, url, headers, stream):
        b''.join(stream)
        return aihub_upload.Response(200, b'{"url":"https://stage.example/source.pdf"}')
    def task(request):
        raise doc2x.Doc2XReason('doc2x_poll_budget_exhausted', 'budget exhausted')
    services = {'upload_transport': upload, 'doc2x_transport': task}
    out = command(['run', '--source', str(pdf_file), '--output-dir', str(tmp_path / 'out'),
                   '--visual-capability', 'available'], services)
    monkeypatch.setenv('AIHUB_API_KEY', 'second-test-key')
    resumed = command(['resume', '--work-bundle', out['work_bundle']], services)
    assert resumed['reason']['code'] == 'credential_source_changed'


def test_stale_action_and_source_tampering_are_rejected(pdf_file, tmp_path):
    z = tmp_path / 'existing.zip'
    z.write_bytes(zip_bytes('# Text\n\nMore text\n'))
    out = command(['run', '--source', str(pdf_file), '--result-zip', str(z),
                   '--output-dir', str(tmp_path / 'out'), '--visual-capability', 'available'])
    action = out['action']
    response = tmp_path / 'response.json'
    response.write_text(json.dumps(review_payload(action)))
    args = ['record-review', '--work-bundle', out['work_bundle'], '--action-id', action['action_id'], '--input', str(response)]
    assert command(args)['outcome'] == 'host_action_required'
    assert command(args)['reason']['code'] == 'stale_action'
    frozen = Path(out['work_bundle']) / '01-source/source.pdf'
    frozen.write_bytes(frozen.read_bytes() + b'tampered')
    assert command(['resume', '--work-bundle', out['work_bundle']])['outcome'] == 'blocked'


def test_page_findings_are_persisted_and_disclosed(pdf_file, tmp_path):
    z = tmp_path / 'existing.zip'
    z.write_bytes(zip_bytes('# Content\n'))
    out = command(['run', '--source', str(pdf_file), '--result-zip', str(z),
                   '--output-dir', str(tmp_path / 'out'), '--visual-capability', 'available'])
    response = tmp_path / 'response.json'
    response.write_text(json.dumps(review_payload(out['action'], findings=['A source paragraph is entirely absent.'])))
    out = command(['record-review', '--work-bundle', out['work_bundle'], '--action-id', out['action']['action_id'], '--input', str(response)])
    out, _ = finish(out, tmp_path)
    assert out['quality'] == 'needs_attention'
    assert 'entirely absent' in json.dumps(out['warnings'])


def test_version_is_one_value():
    import tomllib
    import yaml
    skill = Path(__file__).parents[1]
    project = tomllib.loads((skill / 'pyproject.toml').read_text())['project']
    front = yaml.safe_load((skill / 'SKILL.md').read_text().split('---')[1])
    assert front['metadata']['version'] == project['version']
    assert front['description'].startswith('v%s｜' % project['version'])


def test_changes_during_final_recheck_require_attention_and_preserve_each_response(pdf_file, tmp_path):
    z = tmp_path / 'existing.zip'
    z.write_bytes(zip_bytes('# Volatge: 5 V\n'))
    out = command(['run', '--source', str(pdf_file), '--result-zip', str(z),
                   '--output-dir', str(tmp_path / 'out'), '--visual-capability', 'available'])
    for _ in range(4):
        if out['outcome'] != 'host_action_required':
            break
        action = out['action']
        payload = review_payload(action, repair=True)
        for item, block in zip(payload['reviews'], action['blocks']):
            if 'Voltage' in block['markdown_fragment']:
                item.update(verdict='altered', evidence_page=action['page_number'],
                            evidence_bbox=[0, 0, 1, 1], replacement_text='# Voltage: 5 V\n')
        response = tmp_path / 'response.json'
        response.write_text(json.dumps(payload))
        out = command(['record-review', '--work-bundle', out['work_bundle'],
                       '--action-id', action['action_id'], '--input', str(response)])
    assert out['outcome'] == 'completed'
    assert out['quality'] == 'needs_attention'
    assert any(w['kind'] == 'final_revision_needs_review' for w in out['warnings'])
    report = json.loads(Path(out['artifacts']['review.json']).read_text())
    assert len(report['responses']) == 3
    assert report['responses'][0]['reviews'][0]['replacement_text'].startswith('# Voltage')


def test_inspect_does_not_require_pandoc_or_write_checkpoints(pdf_file, tmp_path, monkeypatch):
    z = tmp_path / 'existing.zip'
    z.write_bytes(zip_bytes('# Text\n'))
    out = command(['run', '--source', str(pdf_file), '--result-zip', str(z),
                   '--output-dir', str(tmp_path / 'out'), '--visual-capability', 'available'])
    work = Path(out['work_bundle'])
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (work / '.state').glob('*.json')}
    monkeypatch.setattr(cli, 'preflight', lambda _: pytest.fail('inspect must not probe dependencies'))
    assert command(['inspect', '--work-bundle', str(work)])['outcome'] == 'ready'
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}


def test_poll_budget_resume_queries_same_task_without_upload_or_create(pdf_file, tmp_path, monkeypatch):
    monkeypatch.setenv('AIHUB_API_KEY', 'test-key')
    calls, uploads = [], []
    clock = [0]
    complete = [False]
    def upload(method, url, headers, stream):
        b''.join(stream)
        uploads.append(url)
        return aihub_upload.Response(200, b'{"url":"https://stage.example/source.pdf"}')
    def task(request):
        calls.append(request['method'])
        body = {'id': 'same-task', 'model': 'doc2x-v3', 'type': 'document', 'status': 'processing'}
        if complete[0]:
            body.update(status='completed', results=[{'url': 'https://result.example/result.zip'}])
        return 200, json.dumps(body).encode()
    services = {'upload_transport': upload, 'doc2x_transport': task,
                'monotonic': lambda: clock[0], 'sleep': lambda t: clock.__setitem__(0, clock[0] + t),
                'download_transport': FakeDownload([FakeResponse(zip_bytes('# Text\n'))])}
    out = command(['run', '--source', str(pdf_file), '--output-dir', str(tmp_path / 'out'),
                   '--visual-capability', 'available'], services)
    assert out['reason']['raw_reason'] == 'doc2x_poll_budget_exhausted'
    assert clock[0] <= 600
    complete[0] = True
    out = command(['resume', '--work-bundle', out['work_bundle']], services)
    assert out['outcome'] == 'host_action_required'
    assert len(uploads) == calls.count('POST') == 1
