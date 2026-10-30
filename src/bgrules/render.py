"""Render rulebook pages as PNGs with cited passages highlighted.

Bounding boxes come from extraction (pdfplumber: PDF points, origin at the
media box's top-left). pypdfium2 renders only the crop box, so boxes are moved
by the crop box's offset before scaling; most PDFs have none.
"""

import io
import re
import unicodedata

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

from .store import quote_fragments, quote_key

# Blue: rulebook pages are often yellow or cream (Catan's are). The white edge
# keeps marks visible on dark pages. Marks go beside or under the text, never
# over it, so the printed colours stay as they are.
MARK = (0, 80, 220)
MARK_EDGE = (255, 255, 255)
STACK_GAP = 12  # pixels between stacked crops


def merge_regions(regions: list[tuple[float, float, float, float]]) -> list[tuple[float, float, float, float]]:
    """Merge overlapping crop regions, keeping reading order (by each group's first region).

    Text-box layouts give many small boxes for one passage; cropping each
    separately would repeat the same text in overlapping crops. Columns stay
    separate crops, so a passage that continues in the next column still stacks.
    """
    groups = [list(r) + [i] for i, r in enumerate(regions)]
    merged = True
    while merged:
        merged = False
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                ga, gb = groups[a], groups[b]
                if ga[0] < gb[2] and gb[0] < ga[2] and ga[1] < gb[3] and gb[1] < ga[3]:
                    groups[a] = [min(ga[0], gb[0]), min(ga[1], gb[1]), max(ga[2], gb[2]), max(ga[3], gb[3]), min(ga[4], gb[4])]
                    del groups[b]
                    merged = True
                    break
            if merged:
                break
    return [tuple(g[:4]) for g in sorted(groups, key=lambda g: g[4])]


def _key(text: str) -> str:
    # Hyphens are dropped too: a word split across lines ("con-" / "tinue") may
    # have been joined without its hyphen in the passage text.
    return quote_key(unicodedata.normalize("NFKC", text)).replace("-", "")


def quote_lines(lines: list[dict], quote: str) -> dict | None:
    """Where `quote` is in a passage's `lines` (as stored by the index): a box per
    line to underline, trimmed to the quoted part of the first and last lines.
    None when it isn't there. Fragments are matched in order, as verify_quote does.

    `highlight` is "exact" when the boxes are the lines' own, or "approximate"
    with `reasons`: "partial_line" (a line's quoted part is estimated from its
    share of characters), "ocr" (positions read from a page image) or
    "repeated" (the quote's start appears more than once; the first is used)."""
    fragments = [k for k in (_key(f) for f in quote_fragments(quote)) if k]
    if not fragments:
        return None
    keys = [_key(ln["text"]) for ln in lines]
    text = "".join(keys)
    owner = [(i, j) for i, k in enumerate(keys) for j in range(len(k))]  # text[n] is keys[i][j]
    covered: dict[int, list[int]] = {}
    pos = 0
    for frag in fragments:
        at = text.find(frag, pos)
        if at < 0:
            return None
        for i, j in owner[at:at + len(frag)]:
            lo, hi = covered.get(i, [j, j])
            covered[i] = [min(lo, j), max(hi, j)]
        pos = at + len(frag)
    reasons = set()
    boxes = []
    for i, (lo, hi) in sorted(covered.items()):
        key, (x0, top, x1, bottom) = keys[i], lines[i]["bbox"]
        # Punctuation the quote left off (a final full stop) still counts as covered.
        lo = 0 if re.fullmatch(r"\W*", key[:lo]) else lo
        hi = len(key) - 1 if re.fullmatch(r"\W*", key[hi + 1:]) else hi
        if (lo, hi) != (0, len(key) - 1):
            reasons.add("partial_line")
            x0, x1 = x0 + (x1 - x0) * lo / len(key), x0 + (x1 - x0) * (hi + 1) / len(key)
        if lines[i].get("ocr"):
            reasons.add("ocr")
        boxes.append({"page": lines[i]["page"], "bbox": [x0, top, x1, bottom]})
    if text.count(fragments[0]) > 1:
        reasons.add("repeated")
    return {"boxes": boxes, "highlight": "approximate" if reasons else "exact", "reasons": sorted(reasons)}


def _stack(images: list[Image.Image]) -> Image.Image:
    stacked = Image.new("RGB", (max(i.width for i in images), sum(i.height for i in images) + STACK_GAP * (len(images) - 1)), "white")
    y = 0
    for i in images:
        stacked.paste(i, (0, y))
        y += i.height + STACK_GAP
    return stacked


def stack_pngs(pngs: list[bytes]) -> bytes:
    """One PNG from several, top to bottom: a quote that runs over a page, as a single image."""
    if len(pngs) == 1:
        return pngs[0]
    buf = io.BytesIO()
    _stack([Image.open(io.BytesIO(p)).convert("RGB") for p in pngs]).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def crop_offset(page: pdfium.PdfPage) -> tuple[float, float]:
    """How far the crop box's top-left is from the media box's, in points:
    subtract it from an extracted box to place it on the rendered page."""
    media, crop = page.get_mediabox(), page.get_cropbox()
    return crop[0] - media[0], media[3] - crop[3]


def render(pdf_path: str, page: int, boxes: list[list[float]], scale: float = 1.5, crop_margin: float | None = None,
           underline: list[list[float]] | None = None) -> bytes:
    """Render one page (1-based) with `boxes` marked by a bar in the margin
    beside each, or, with `underline` set (a quote within them), those lines
    underlined instead.

    With crop_margin set, crop each box (or each underlined line, so a phone
    shows the quote large rather than its whole passage) plus that margin, in points,
    separately and stack the crops top to bottom in reading order, so a
    passage that continues in the next column stays a compact image;
    otherwise return the whole page.
    """
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        img = pdf[page - 1].render(scale=scale).to_pil().convert("RGB")
        dx, dy = crop_offset(pdf[page - 1])
    finally:
        pdf.close()
    if dx or dy:
        boxes = [[x0 - dx, top - dy, x1 - dx, bottom - dy] for x0, top, x1, bottom in boxes]
        underline = underline and [[x0 - dx, top - dy, x1 - dx, bottom - dy] for x0, top, x1, bottom in underline]

    draw = ImageDraw.Draw(img)

    def mark(points, width):
        draw.line(points, fill=MARK_EDGE, width=width + 2 * max(1, round(scale)))
        draw.line(points, fill=MARK, width=width)
    if underline:
        for x0, top, x1, bottom in underline:
            y = (bottom + 0.5) * scale  # below the descenders, so the edge never cuts into letters
            mark([(x0 * scale, y), (x1 * scale, y)], max(2, round(scale * 1.5)))
    else:
        for x0, top, x1, bottom in boxes:
            x = max(2, x0 - 7) * scale
            mark([(x, top * scale), (x, bottom * scale)], max(3, round(scale * 2.5)))

    if crop_margin is not None and boxes:
        w, h = img.size
        regions = merge_regions([
            (max(0, (x0 - crop_margin) * scale), max(0, (top - crop_margin) * scale),
             min(w, (x1 + crop_margin) * scale), min(h, (bottom + crop_margin) * scale))
            for x0, top, x1, bottom in (underline or boxes)
        ])
        img = _stack([img.crop(tuple(round(v) for v in r)) for r in regions])

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
