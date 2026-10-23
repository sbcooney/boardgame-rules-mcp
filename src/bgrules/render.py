"""Render rulebook pages as PNGs with cited passages highlighted.

Bounding boxes come from extraction (pdfplumber: PDF points, origin top-left),
which matches pypdfium2's rendered bitmap once scaled.
"""

import io
import unicodedata

import pdfplumber
import pypdfium2 as pdfium
from PIL import Image, ImageDraw

from .store import quote_fragments, quote_key

# Blue, not yellow: rulebook pages are often yellow or cream (Catan's are).
HIGHLIGHT_FILL = (0, 110, 255, 40)
HIGHLIGHT_EDGE = (0, 80, 220, 255)
CONTEXT_FILL = (0, 110, 255, 22)  # the rest of the passage, when a quote is highlighted
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
    # have been joined without its hyphen in the indexed text.
    return quote_key(unicodedata.normalize("NFKC", text)).replace("-", "")


def quote_boxes(pdf_path: str, spans: list[dict], quote: str) -> list[dict] | None:
    """Boxes, one per printed line, around the words of `quote` within a passage's
    `spans`; None when the quote isn't found there (or the page has no text layer,
    as on OCR'd pages). Fragments are matched in order, as verify_quote does."""
    fragments = [_key(f) for f in quote_fragments(quote)]
    if not any(fragments):
        return None
    words: list[tuple[int, dict]] = []  # (pdf page, word), in reading order
    with pdfplumber.open(pdf_path) as pdf:
        for span in spans:
            x0, top, x1, bottom = span["bbox"]
            inside = [w for w in pdf.pages[span["page"] - 1].extract_words()
                      if x0 - 1 <= (w["x0"] + w["x1"]) / 2 <= x1 + 1 and top - 1 <= (w["top"] + w["bottom"]) / 2 <= bottom + 1]
            lines: list[list[dict]] = []
            for w in sorted(inside, key=lambda w: (w["top"], w["x0"])):
                if lines and abs(lines[-1][0]["top"] - w["top"]) < 2.5:
                    lines[-1].append(w)
                else:
                    lines.append([w])
            words += [(span["page"], w) for line in lines for w in sorted(line, key=lambda w: w["x0"])]
    text, owner = "", []  # owner[i]: index in `words` of text[i]
    for i, (_, w) in enumerate(words):
        k = _key(w["text"])
        text += k
        owner += [i] * len(k)
    matched: set[int] = set()
    pos = 0
    for frag in filter(None, fragments):
        at = text.find(frag, pos)
        if at < 0:
            return None
        matched.update(owner[at:at + len(frag)])
        pos = at + len(frag)
    boxes: list[dict] = []
    for i in sorted(matched):
        page, w = words[i]
        last = boxes[-1] if boxes else None
        if last and last["page"] == page and abs(last["bbox"][1] - w["top"]) < 2.5 and i - 1 in matched:
            b = last["bbox"]
            last["bbox"] = [min(b[0], w["x0"]), min(b[1], w["top"]), max(b[2], w["x1"]), max(b[3], w["bottom"])]
        else:
            boxes.append({"page": page, "bbox": [w["x0"], w["top"], w["x1"], w["bottom"]]})
    return boxes


def render(pdf_path: str, page: int, boxes: list[list[float]], scale: float = 1.5, crop_margin: float | None = None,
           focus: list[list[float]] | None = None) -> bytes:
    """Render one page (1-based) with `boxes` highlighted.

    With `focus` set, those boxes (a quote within the passage) are highlighted
    and `boxes` only lightly shaded for context; crops still follow `boxes`.
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
    def rect(x0, top, x1, bottom):
        return [(x0 - pad) * scale, (top - pad) * scale, (x1 + pad) * scale, (bottom + pad) * scale]
    if focus:
        for box in boxes:
            draw.rectangle(rect(*box), fill=CONTEXT_FILL)
    for box in focus or boxes:
        draw.rectangle(rect(*box), fill=HIGHLIGHT_FILL, outline=HIGHLIGHT_EDGE, width=max(2, round(scale * 1.5)))
    img = Image.alpha_composite(img, overlay).convert("RGB")

    if crop_margin is not None and boxes:
        w, h = img.size
        regions = merge_regions([
            (max(0, (x0 - crop_margin) * scale), max(0, (top - crop_margin) * scale),
             min(w, (x1 + crop_margin) * scale), min(h, (bottom + crop_margin) * scale))
            for x0, top, x1, bottom in boxes
        ])
        crops = [img.crop(tuple(round(v) for v in r)) for r in regions]
        stacked = Image.new("RGB", (max(c.width for c in crops), sum(c.height for c in crops) + STACK_GAP * (len(crops) - 1)), "white")
        y = 0
        for c in crops:
            stacked.paste(c, (0, y))
            y += c.height + STACK_GAP
        img = stacked

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
