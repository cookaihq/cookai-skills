"""The public-PDF boundary through the v2 entry and shared HTTPS transport."""
import json
from pathlib import Path

import pytest

import pdf_source
from pdf2markdown import network
from .test_workflow_v2 import FakeDownload, FakeResponse, command, pdf_file, zip_bytes


@pytest.mark.parametrize('url', ['http://docs.example/a.pdf', 'file:///etc/passwd',
                                'https://user:password@docs.example/a.pdf',
                                'https://docs.example/a.pdf\r\nInjected: yes'])
def test_rejected_url_never_reaches_dns(tmp_path, url):
    transport = FakeDownload([])
    with pytest.raises(network.DownloadError) as error:
        network.download(url, tmp_path / 'source', 100, transport=transport)
    assert error.value.category == "rejected"
    assert not transport.calls


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1'])
def test_private_endpoints_never_receive_request(tmp_path, address):
    transport = FakeDownload([], address=address)
    with pytest.raises(network.DownloadError, match='unsafe_source_address'):
        network.download('https://docs.example/a.pdf', tmp_path / 'source', 100, transport=transport)
    assert not transport.calls


def test_public_pdf_redirect_freeze_and_redacted_result(pdf_file, tmp_path):
    archive = tmp_path / 'existing.zip'
    archive.write_bytes(zip_bytes('# Text\n'))
    source = pdf_file.read_bytes()
    transport = FakeDownload([
        FakeResponse(b'', 302, [('Location', '/final.pdf?signed=secret')]),
        FakeResponse(source, headers=[('Content-Length', str(len(source))), ('Content-Type', 'application/pdf')]),
    ])
    out = command(['run', '--source', 'https://docs.example/a.pdf?key=hidden',
                   '--result-zip', str(archive), '--output-dir', str(tmp_path / 'out'),
                   '--visual-capability', 'available'], {'download_transport': transport})
    assert out['outcome'] == 'host_action_required', out
    assert len(transport.calls) == 2
    assert (Path(out['work_bundle']) / '01-source/source.pdf').read_bytes() == source
    assert 'signed=secret' not in json.dumps(out)
    assert 'key=hidden' not in json.dumps(out)


def test_declared_length_and_actual_pdf_parser_are_required(tmp_path):
    for body, headers in [
        (b'%PDF-fake', [('Content-Type', 'text/html')]),
        (b'not-pdf', [('Content-Type', 'application/pdf')]),
        (b'%PDF-fake', [('Content-Type', 'application/pdf')]),
        (b'%PDF-fake', [('Content-Type', 'application/pdf'), ('Content-Length', '9999')]),
    ]:
        transport = FakeDownload([FakeResponse(body, headers=headers)])
        with pytest.raises(network.DownloadError):
            network.download('https://docs.example/a.pdf', tmp_path / 'source', 100,
                             transport=transport, pdf=True, attempts=1)
        assert not (tmp_path / 'source').exists()
