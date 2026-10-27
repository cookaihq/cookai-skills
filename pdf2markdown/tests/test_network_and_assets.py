from pathlib import Path

import pytest

from pdf2markdown import assets, export, network, viewer
from .test_workflow_v2 import FakeDownload, FakeResponse


def test_transient_http_retried_and_private_redirect_rejected(tmp_path):
    transport = FakeDownload([FakeResponse(b'', 503), FakeResponse(b'complete')])
    waits = []
    network.download('https://public.example/result.zip?key=hidden', tmp_path / 'r.zip', 100,
                     transport=transport, sleep=waits.append)
    assert waits == [1]
    assert (tmp_path / 'r.zip').read_bytes() == b'complete'
    private = FakeDownload([FakeResponse(b'private')], address='127.0.0.1')
    with pytest.raises(network.DownloadError, match='unsafe_source_address'):
        network.download('https://public.example/result.zip', tmp_path / 'blocked', 100, transport=private)
    assert not private.calls
    assert not (tmp_path / 'blocked').exists()


def test_deterministic_4xx_not_retried_and_response_bounds_clean_partial_file(tmp_path):
    transport = FakeDownload([FakeResponse(b'', 403)])
    with pytest.raises(network.DownloadError, match='403'):
        network.download('https://public.example/r', tmp_path / 'r', 100, transport=transport,
                         sleep=lambda _: pytest.fail('403 must not retry'))
    assert len(transport.calls) == 1
    with pytest.raises(network.DownloadError, match='size_limit'):
        network.download('https://public.example/r', tmp_path / 'r', 2,
                         transport=FakeDownload([FakeResponse(b'large')]))
    assert not (tmp_path / 'r').exists()


def test_retry_after_is_finite_and_capped(tmp_path):
    transport = FakeDownload([FakeResponse(b'', 429, [('Retry-After', '999')]), FakeResponse(b'ok')])
    waits = []
    network.download('https://public.example/r', tmp_path / 'r', 100, transport=transport, sleep=waits.append)
    assert waits == [60]
    assert network.retry_after('nan') is None
    assert network.retry_after('inf') is None


def test_images_with_titles_references_and_html_are_localized():
    raw = b'![a](https://cdn.example/a.png "caption")\n\n![b][img]\n\n[img]: https://cdn.example/a.png\n\n<img src="https://cdn.example/a.png">\n'
    replaced = assets.replace_image_targets(raw, {'https://cdn.example/a.png': 'images/a.png'}, assets.find_pandoc())
    assert b'https://' not in replaced
    assert assets.discover_image_targets(replaced, assets.find_pandoc()) == ['images/a.png']


def test_viewer_strips_active_content_and_can_restore_hidden_columns():
    raw = b'# Title\n\n<script>alert(1)</script><iframe src="https://evil.example"></iframe><img src="https://evil.example/a" onerror="bad()">\n'
    rendered = viewer.render('Title', raw, raw, assets.find_pandoc(), []).decode()
    assert 'alert(1)' not in rendered
    assert 'evil.example' not in rendered
    assert 'onerror' not in rendered
    from lxml import html
    tree = html.fromstring(rendered)
    assert len(tree.xpath('//nav/button[@data-toggle]')) == 3
    assert not tree.xpath('//section//button[@data-toggle]')


def test_local_asset_path_rejects_encoded_traversal_and_symlink(tmp_path):
    outside = tmp_path / 'outside.png'
    outside.write_bytes(b'private')
    root = tmp_path / 'tree'
    root.mkdir()
    (root / 'image.png').symlink_to(outside)
    for target in ('%2e%2e/outside.png', 'image.png', 'file:///etc/passwd'):
        with pytest.raises((assets.AssetError, OSError)):
            export.local_asset(root, root, target)


def test_image_rewriting_preserves_code_and_ordinary_links():
    raw = (b'![real](old.png)\n\n`![example](old.png)`\n\n'
           b'```md\n![example](old.png)\n```\n\n[ordinary](old.png)\n')
    result = assets.replace_image_targets(raw, {'old.png': 'images/new.png'}, assets.find_pandoc())
    assert result == raw.replace(b'![real](old.png)', b'![real](images/new.png)')


def test_shared_image_link_reference_is_not_silently_changed():
    raw = b'![image][ref] [ordinary link][ref]\n\n[ref]: old.png\n'
    with pytest.raises(assets.AssetError, match='non-image content'):
        assets.replace_image_targets(raw, {'old.png': 'images/new.png'}, assets.find_pandoc())
