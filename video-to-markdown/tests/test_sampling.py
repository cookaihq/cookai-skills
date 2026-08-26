from pathlib import Path

from PIL import Image

from video_to_markdown import sampling


def test_batches_never_exceed_five_minutes():
    batches = sampling.split_batches(725_000)
    assert [(item["start_ms"], item["end_ms"]) for item in batches] == [
        (0, 300_000),
        (300_000, 600_000),
        (600_000, 725_000),
    ]


def test_contact_sheet_is_a_rendered_image(tmp_path: Path):
    frames = []
    for index in range(2):
        path = tmp_path / ("frame-%d.jpg" % index)
        Image.new("RGB", (320, 180), (index * 100, 30, 60)).save(path)
        frames.append(
            {
                "path": str(path),
                "timestamp": "00:00:0%d.000" % index,
                "frame_id": "frame-%012d" % (index * 1000),
            }
        )
    sheets = sampling.create_contact_sheets(frames, tmp_path, "contact")
    with Image.open(sheets[0]["path"]) as image:
        assert image.size == (1920, 912)
