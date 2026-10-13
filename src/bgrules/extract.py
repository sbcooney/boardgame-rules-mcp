"""Turn a rulebook PDF into sections of verbatim text with page locations.

Driven by the per-document `extraction` hints in the manifest (see M0 findings):
pages are split into columns before line extraction, only heading/body fonts
are kept, and a section runs from one heading to the next, across columns and
pages. Long sections are split into passages so search results stay specific.
"""

import re
from dataclasses import dataclass, field

import pdfplumber

PASSAGE_TARGET_CHARS = 700


@dataclass
class Line:
    text: str
    page: int  # 1-based PDF page
    column: int  # 0-based column on the page
    bbox: tuple[float, float, float, float]  # x0, top, x1, bottom in PDF points
    is_heading: bool


@dataclass
class Passage:
    lines: list[Line]
    heading_lines: list[Line] = field(default_factory=list)  # highlighted, not part of the text

    @property
    def page(self) -> int:
        return self.lines[0].page

    @property
    def text(self) -> str:
        return join_lines([ln.text for ln in self.lines])

    @property
    def spans(self) -> list[dict]:
        return spans(self.heading_lines + self.lines)


@dataclass
class Section:
    heading: str
    page_start: int
    part: str | None = None  # top-level division, e.g. "Game Rules" or "Almanac"
    lines: list[Line] = field(default_factory=list)
    heading_lines: list[Line] = field(default_factory=list)

    @property
    def path(self) -> list[str]:
        return [self.part, self.heading] if self.part else [self.heading]

    @property
    def page_end(self) -> int:
        return self.lines[-1].page if self.lines else self.page_start

    @property
    def text(self) -> str:
        return join_lines([ln.text for ln in self.lines])

    @property
    def spans(self) -> list[dict]:
        return spans(self.heading_lines + self.lines)

    def passages(self, target_chars: int = PASSAGE_TARGET_CHARS) -> list[Passage]:
        """Split at sentence-ending lines once a passage reaches target_chars."""
        out: list[Passage] = []
        current: list[Line] = []
        size = 0
        for ln in self.lines:
            current.append(ln)
            size += len(ln.text) + 1
            if size >= target_chars and ln.text.rstrip().endswith((".", "!", "?", ".”", ":")):
                out.append(Passage(current))
                current, size = [], 0
        if current:
            # Fold a short tail into the previous passage rather than leave a fragment.
            if out and size < target_chars / 3:
                out[-1].lines.extend(current)
            else:
                out.append(Passage(current))
        if out:
            out[0].heading_lines = list(self.heading_lines)
        return out


def spans(lines: list[Line]) -> list[dict]:
    """One bounding box per (page, column) the lines cover, for highlighting."""
    boxes: dict[tuple[int, int], list[float]] = {}
    for ln in lines:
        x0, top, x1, bottom = ln.bbox
        box = boxes.setdefault((ln.page, ln.column), [x0, top, x1, bottom])
        box[0], box[1] = min(box[0], x0), min(box[1], top)
        box[2], box[3] = max(box[2], x1), max(box[3], bottom)
    return [{"page": page, "bbox": [round(v, 1) for v in b]} for (page, _), b in boxes.items()]


def join_lines(lines: list[str]) -> str:
    """Join wrapped lines into running text, undoing end-of-line hyphenation and
    the stray space a dropped icon leaves before punctuation."""
    out = ""
    for text in lines:
        text = text.strip()
        if not text:
            continue
        if out.endswith("-") and text[:1].islower():
            out = out[:-1] + text
        elif out:
            out += " " + text
        else:
            out = text
    return re.sub(r" +([.,;:!?])", r"\1", out)


def normalize_heading(text: str) -> str:
    """Small-caps fonts extract as mixed case ("lonGest road"); title-case them."""
    small = {"a", "an", "and", "of", "the", "to", "in", "on", "or", "for"}

    def fix(m: re.Match) -> str:
        word = m.group(0)
        if m.start() > 0 and word.lower() in small:
            return word.lower()
        return word.capitalize()

    return re.sub(r"[A-Za-z]+", fix, " ".join(text.split()))


FULL_WIDTH = -1  # Line.column for text that spans the page's columns
GUTTER_MIN_GAP = 12.0  # points: a column gutter is wider than any word space


def _matches(fontname: str, names: list[str]) -> bool:
    return any(n in fontname for n in names)


def _char_key(c) -> tuple:
    return (round(c["x0"], 2), round(c["top"], 2), c["text"])


def _is_full_width(line: dict, mid: float) -> bool:
    """True for a line that runs across the gutter, as opposed to two column
    lines that merely sit at the same height (those have a wide gap at mid)."""
    if not (line["x0"] < mid - 20 and line["x1"] > mid + 20):
        return False
    chars = sorted(line["chars"], key=lambda c: c["x0"])
    gaps = [b["x0"] - a["x1"] for a, b in zip(chars, chars[1:]) if a["x1"] <= mid + 20 and b["x0"] >= mid - 20]
    return max(gaps, default=0.0) < GUTTER_MIN_GAP


def extract_lines(pdf_path: str, hints: dict) -> list[Line]:
    columns = hints.get("columns", 1)
    heading_fonts = hints.get("heading_fonts", [])
    body_fonts = hints.get("body_fonts", [])
    body_min, body_max = hints.get("body_size", [0, 1_000])
    first, last = hints.get("content_pages", [1, 10_000])
    pages = range(first, last + 1)

    def is_heading_char(c) -> bool:
        return any(
            h["name"] in c["fontname"] and h.get("min_size", 0) - 0.1 <= c["size"] <= h.get("max_size", 1_000) + 0.1
            for h in heading_fonts
        )

    def keep(obj) -> bool:
        if obj["object_type"] != "char":
            return True
        if is_heading_char(obj):
            return True
        # Body text only in body fonts at body sizes: drops card art, sidebars,
        # decorative borders, watermarks and oversized diagram symbols.
        return (not body_fonts or _matches(obj["fontname"], body_fonts)) and body_min - 0.1 <= obj["size"] <= body_max + 0.1

    def to_lines(raw: list[dict], page_no: int, column: int) -> list[Line]:
        out = []
        for ln in raw:
            chars = ln["chars"]
            heading = sum(is_heading_char(c) for c in chars) > len(chars) / 2
            if heading and not re.search(r"[A-Za-z]", ln["text"]):
                continue  # heading-font symbols in diagrams ("=", "+")
            if re.fullmatch(r"[A-Z]", ln["text"].strip()):
                continue  # alphabetical divider letters between almanac entries
            out.append(Line(text=ln["text"], page=page_no, column=column, bbox=(ln["x0"], ln["top"], ln["x1"], ln["bottom"]), is_heading=heading))
        return out

    lines: list[Line] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            if page_no not in pages:
                continue
            page = page.dedupe_chars().filter(keep)
            if columns == 1:
                lines += to_lines(page.extract_text_lines(return_chars=True), page_no, 0)
                continue

            # Full-width lines (an intro paragraph across both columns) are read
            # whole; the stretches between them are read column by column, so
            # reading order stays top to bottom.
            mid = page.width / 2
            full = [ln for ln in page.extract_text_lines(return_chars=True) if _is_full_width(ln, mid)]
            full_keys = {_char_key(c) for ln in full for c in ln["chars"]}
            cols_only = page.filter(lambda o: o["object_type"] != "char" or _char_key(o) not in full_keys)
            width = page.width / columns

            y = 0.0
            for band in [*full, None]:
                y_end = band["top"] if band else page.height
                if y_end > y:
                    for col in range(columns):
                        region = cols_only.crop((col * width, y, (col + 1) * width, y_end))
                        lines += to_lines(region.extract_text_lines(return_chars=True), page_no, col)
                if band:
                    lines += to_lines([band], page_no, FULL_WIDTH)
                    y = max(y, band["bottom"])
    return lines


def build_sections(lines: list[Line], parts: list[dict] | None = None, preamble_heading: str = "Introduction") -> list[Section]:
    """Group lines into sections. `parts` (from the manifest) label each section's
    top-level division; a part begins at the section whose heading is its
    `first_heading` (the first part needs none)."""
    parts = parts or []
    part = parts[0]["name"] if parts else None
    sections: list[Section] = []
    current: Section | None = None
    for ln in lines:
        if ln.is_heading:
            if current is not None and not current.lines:
                # Heading wrapped onto a second line: merge into the same heading.
                current.heading = f"{current.heading} {normalize_heading(ln.text)}"
                current.heading_lines.append(ln)
            else:
                current = Section(heading=normalize_heading(ln.text), page_start=ln.page, part=part, heading_lines=[ln])
                sections.append(current)
        else:
            if current is None:
                current = Section(heading=preamble_heading, page_start=ln.page, part=part)
                sections.append(current)
            current.lines.append(ln)
        # A part may start at a heading that wraps, so check after merging.
        for p in parts[1:]:
            if current is not None and current.heading.lower() == p.get("first_heading", "").lower():
                part = current.part = p["name"]
    return [s for s in sections if s.lines]
