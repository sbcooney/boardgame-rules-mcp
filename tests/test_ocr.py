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
