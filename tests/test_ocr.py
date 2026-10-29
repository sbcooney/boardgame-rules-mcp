"""OCR of image-only pages (scans, photos, "print to PDF" copies)."""

import importlib.util

import pytest
from PIL import Image, ImageDraw, ImageFont

from bgrules.extract import _overlap, build_sections, extract_lines


def test_overlap_is_measured_against_the_smaller_box():
    assert _overlap((0, 0, 10, 10), (5, 0, 15, 10)) == 0.5
    assert _overlap((0, 0, 100, 10), (10, 2, 20, 8)) == 1.0  # a box inside another
    assert _overlap((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0


@pytest.mark.skipif(importlib.util.find_spec("Vision") is None, reason="OCR uses macOS's Vision framework")
def test_image_only_pdf_is_read_by_ocr(tmp_path):
    page = Image.new("RGB", (1275, 1650), "white")  # US letter at 150 dpi
    draw = ImageDraw.Draw(page)
    draw.text((100, 100), "Setup", font=ImageFont.load_default(size=72), fill="black")
    body = ImageFont.load_default(size=32)
    for i, line in enumerate(["Each player takes three workers.", "The youngest player goes first."]):
        draw.text((100, 220 + i * 50), line, font=body, fill="black")
    pdf = tmp_path / "scan.pdf"
    page.save(pdf, resolution=150)

    lines = extract_lines(str(pdf), {})  # no hints: any page without text is OCR'd
    assert all(ln.ocr for ln in lines)
    [section] = build_sections(lines)
    assert section.heading == "Setup"  # tallest line, by the default height rule
    assert section.text == "Each player takes three workers. The youngest player goes first."


@pytest.mark.skipif(importlib.util.find_spec("Vision") is None, reason="OCR uses macOS's Vision framework")
def test_ocr_underline_sits_under_the_text_when_the_page_is_cropped(tmp_path):
    # OCR reads the rendered crop box; its boxes must be stored from the media box like pdfplumber's (#40).
    import io

    import pypdfium2 as pdfium

    from bgrules.render import MARK, render

    page = Image.new("RGB", (1275, 1650), "white")
    ImageDraw.Draw(page).text((150, 220), "The youngest player goes first.", font=ImageFont.load_default(size=32), fill="black")
    scan = tmp_path / "scan.pdf"
    page.save(scan, resolution=150)
    doc = pdfium.PdfDocument(str(scan))
    doc[0].set_cropbox(30, 30, 582, 762)
    pdf = tmp_path / "cropped.pdf"
    doc.save(str(pdf))
    doc.close()

    [line] = extract_lines(str(pdf), {})
    img = Image.open(io.BytesIO(render(str(pdf), 1, [list(line.bbox)], scale=1, underline=[list(line.bbox)]))).convert("RGB")
    px = img.load()
    blue = [(x, y) for y in range(img.height) for x in range(img.width) if px[x, y] == MARK]
    ink = [(x, y) for y in range(img.height) for x in range(img.width) if max(px[x, y]) < 100]
    assert blue and ink
    assert 0 <= min(y for _, y in blue) - max(y for _, y in ink) <= 4  # just below the letters
    assert abs(min(x for x, _ in blue) - min(x for x, _ in ink)) <= 3
