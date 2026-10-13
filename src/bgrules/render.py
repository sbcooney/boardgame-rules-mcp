"""Render rulebook pages as PNGs with cited passages highlighted.

Bounding boxes come from extraction (pdfplumber: PDF points, origin top-left),
which matches pypdfium2's rendered bitmap once scaled.
"""

import io

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

# Blue, not yellow: rulebook pages are often yellow or cream (Catan's are).
HIGHLIGHT_FILL = (0, 110, 255, 40)
HIGHLIGHT_EDGE = (0, 80, 220, 255)
STACK_GAP = 12  # pixels between stacked crops


def render(pdf_path: str, page: int, boxes: list[list[float]], scale: float = 1.5, crop_margin: float | None = None) -> bytes:
    """Render one page (1-based) with `boxes` highlighted.

    With crop_margin set, crop each box (plus that margin, in points)
    separately and stack the crops top to bottom in reading order, so a
    passage that continues in the next column stays a compact image;
    otherwise return the whole page.
    """
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        img = pdf[page - 1].render(scale=scale).to_pil().convert("RGBA")
    finally:
        pdf.close()

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    pad = 3
    for x0, top, x1, bottom in boxes:
        rect = [(x0 - pad) * scale, (top - pad) * scale, (x1 + pad) * scale, (bottom + pad) * scale]
        draw.rectangle(rect, fill=HIGHLIGHT_FILL, outline=HIGHLIGHT_EDGE, width=max(2, round(scale * 1.5)))
    img = Image.alpha_composite(img, overlay).convert("RGB")

    if crop_margin is not None and boxes:
        w, h = img.size
        crops = []
        for x0, top, x1, bottom in boxes:
            region = (
                max(0, (x0 - crop_margin) * scale),
                max(0, (top - crop_margin) * scale),
                min(w, (x1 + crop_margin) * scale),
                min(h, (bottom + crop_margin) * scale),
            )
            crops.append(img.crop(tuple(round(v) for v in region)))
        stacked = Image.new("RGB", (max(c.width for c in crops), sum(c.height for c in crops) + STACK_GAP * (len(crops) - 1)), "white")
        y = 0
        for c in crops:
            stacked.paste(c, (0, y))
            y += c.height + STACK_GAP
        img = stacked

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
