from pathlib import Path
import math

import pypdfium2 as pdfium


def inspect(path: Path) -> int:
    with pdfium.PdfDocument(str(path)) as doc:
        if len(doc) < 1:
            raise ValueError("PDF has no pages")
        return len(doc)


def page_range(value: str | None, count: int) -> list[int]:
    if value is None:
        return list(range(1, count + 1))
    pages: set[int] = set()
    for part in value.split(","):
        bounds = part.strip().split("-")
        if len(bounds) not in (1, 2) or not all(x.isdigit() for x in bounds):
            raise ValueError("Use physical pages such as 1,3-5")
        first, last = int(bounds[0]), int(bounds[-1])
        if not 1 <= first <= last <= count:
            raise ValueError("Physical page range is outside the PDF")
        pages.update(range(first, last + 1))
    if not pages:
        raise ValueError("Empty page range")
    return sorted(pages)


def bbox(value):
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("bbox must be [left, top, right, bottom], normalized to 0..1")
    if not all(type(x) in (int, float) and math.isfinite(x) for x in value):
        raise ValueError("bbox must contain finite numbers")
    left, top, right, bottom = value
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("bbox is outside page or empty")
    return left, top, right, bottom


def render(source: Path, page_number: int, destination: Path, dpi=180, crop=None) -> dict:
    if not 72 <= dpi <= 400:
        raise ValueError("dpi must be between 72 and 400")
    with pdfium.PdfDocument(str(source)) as doc:
        page = doc[page_number - 1]
        try:
            width, height = page.get_size()
            if width * height * (dpi / 72) ** 2 > 50_000_000:
                raise ValueError("Page exceeds the 50 megapixel render limit; use lower dpi")
            bitmap = page.render(scale=dpi / 72)
            try:
                image = bitmap.to_pil().copy()
            finally:
                bitmap.close()
            if crop is not None:
                left, top, right, bottom = bbox(crop)
                image = image.crop((math.floor(left * image.width), math.floor(top * image.height),
                                    math.ceil(right * image.width), math.ceil(bottom * image.height)))
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(destination, format="PNG")
            text_page = page.get_textpage()
            try:
                text = text_page.get_text_range()
            finally:
                text_page.close()
            return {"width": image.width, "height": image.height, "pdf_width": width,
                    "pdf_height": height, "dpi": dpi, "text_layer": text}
        finally:
            page.close()
