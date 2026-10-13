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
    is_subheading: bool = False  # a labelled step ("b) Settlement ...") that starts a subsection
    is_part: bool = False  # a page title ("BIRD POWERS") that heads the sections after it


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
    subheading: str | None = None  # set for a subsection: its label line, e.g. "b) Settlement Requires: ..."

    @property
    def path(self) -> list[str]:
        path = [self.part, self.heading] if self.part else [self.heading]
        return path + [self.subheading] if self.subheading else path

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
    sub_rules = hints.get("subheadings", [])
    if isinstance(sub_rules, dict):
        sub_rules = [sub_rules]
    first, last = hints.get("content_pages", [1, 10_000])
    pages = range(first, last + 1)

    def in_size(c, rule) -> bool:
        return rule.get("min_size", 0) - 0.1 <= c["size"] <= rule.get("max_size", 1_000) + 0.1

    def is_heading_char(c) -> bool:
        return any(h["name"] in c["fontname"] and in_size(c, h) for h in heading_fonts)

    def keep_char(c) -> bool:
        if is_heading_char(c) or any(_matches(c["fontname"], r.get("fonts", [])) and in_size(c, r) for r in sub_rules):
            return True
        # Body text only in body fonts at body sizes: drops card art, sidebars,
        # decorative borders, watermarks and oversized diagram symbols.
        # `captions` adds specific font/size bands outside that range (icon
        # captions that name things: food types, habitats, nest types).
        if any(r["name"] in c["fontname"] and in_size(c, r) for r in hints.get("captions", [])):
            return True
        return (not body_fonts or _matches(c["fontname"], body_fonts)) and body_min - 0.1 <= c["size"] <= body_max + 0.1

    def keep(obj) -> bool:
        return obj["object_type"] != "char" or keep_char(obj)

    def is_subheading(ln: dict) -> bool:
        """A labelled step or lead-in that starts a subsection: matches a rule's
        `pattern` (if any) with most characters in the rule's fonts and sizes."""
        chars = ln["chars"]
        for rule in sub_rules:
            if rule.get("pattern") and not re.match(rule["pattern"], ln["text"]):
                continue
            if sum(_matches(c["fontname"], rule.get("fonts", [])) and in_size(c, rule) for c in chars) > len(chars) / 2:
                return True
        return False

    def to_lines(raw: list[dict], page_no: int, column: int) -> list[Line]:
        out = []
        for ln in raw:
            chars = ln["chars"]
            if not chars or not ln["text"].strip():
                continue
            heading = sum(is_heading_char(c) for c in chars) > len(chars) / 2
            if heading and not re.search(r"[A-Za-z]", ln["text"]):
                continue  # heading-font symbols in diagrams ("=", "+")
            if re.fullmatch(r"[A-Z]|\d{1,3}", ln["text"].strip()):
                continue  # almanac divider letters; page numbers
            subheading = not heading and is_subheading(ln)
            part = heading and sum(any(h.get("part") and h["name"] in c["fontname"] and in_size(c, h) for h in heading_fonts) for c in chars) > len(chars) / 2
            out.append(
                Line(
                    text=ln["text"], page=page_no, column=column, bbox=(ln["x0"], ln["top"], ln["x1"], ln["bottom"]),
                    is_heading=heading, is_subheading=subheading, is_part=part,
                )
            )
        return out

    def table_lines(page, page_no: int) -> list[Line]:
        """`layout: table`: rows of names beside shared text (Wingspan's bird
        powers), split into records by the table's horizontal divider lines.
        Within each record, headings and full-width text come first, then the
        cells column by column, so each power sentence stays intact with its
        birds listed before it instead of woven through it."""
        bounds = hints["table"]["column_bounds"]
        words = page.extract_words(extra_attrs=["fontname", "size"])
        words.sort(key=lambda w: (w["top"], w["x0"]))
        rows: list[list[dict]] = []
        for w in words:
            if rows and abs(rows[-1][0]["top"] - w["top"]) < 2.5:
                rows[-1].append(w)
            else:
                rows.append([w])
        cells: list[tuple[int, list[dict]]] = []  # (row index, words)
        for r, row in enumerate(rows):
            row.sort(key=lambda w: w["x0"])
            cells.append((r, [row[0]]))
            for a, b in zip(row, row[1:]):
                # Split at a wide gap, or where a cell inside the table crosses a
                # column edge (power text runs close to the explanation beside it).
                # Cells starting in the first column may run across: those are
                # full-width intro lines.
                inside = cells[-1][1][0]["x0"] >= bounds[0] - 4
                crosses = inside and any(a["x1"] <= bd + 2 and b["x0"] >= bd - 2 for bd in bounds[1:])
                if b["x0"] - a["x1"] > GUTTER_MIN_GAP or crosses:
                    cells.append((r, [b]))
                else:
                    cells[-1][1].append(b)

        seps = sorted({l["top"] for l in page.lines if abs(l["top"] - l["bottom"]) < 1 and l["x0"] >= 0 and l["x1"] - l["x0"] > 50})
        # Order within a column by row, then left to right: an icon glyph
        # mid-sentence ("Discard 1 [egg] from ...") nudges words a fraction of
        # a point up or down, so exact heights would scramble the sentence.
        bands: dict[int, list[tuple[int, tuple, Line]]] = {}
        for r, ws in cells:
            raw = {"text": " ".join(w["text"] for w in ws), "chars": ws,
                   "x0": min(w["x0"] for w in ws), "x1": max(w["x1"] for w in ws),
                   "top": min(w["top"] for w in ws), "bottom": max(w["bottom"] for w in ws)}
            col = sum(raw["x0"] >= b - 4 for b in bounds)
            wide = col == 0 and raw["x1"] > bounds[0] + 20
            for ln in to_lines([raw], page_no, col):
                lead = wide or ln.is_heading or ln.is_subheading
                key = (r, raw["x0"]) if lead else (col, r, raw["x0"])
                bands.setdefault(sum(s <= raw["top"] for s in seps), []).append((0 if lead else 1, key, ln))
        out: list[Line] = []
        for band in sorted(bands):
            items = bands[band]
            # A heading or intro line mid-band starts a new piece: cells above
            # it (the end of the previous record) stay before it.
            lead_rows = sorted(key[0] for kind, key, _ in items if kind == 0)
            def piece(kind, key):
                row = key[0] if kind == 0 else key[1]
                return sum(lr <= row for lr in lead_rows)
            out += [ln for _, _, ln in sorted(items, key=lambda i: (piece(i[0], i[1]), i[0], i[1]))]
        return out

    def layout_for(page_no: int) -> str:
        """`layout` is one mode for the whole document, or a list of
        {pages: [first, last], layout: mode}; pages not listed use columns.
        Modes: columns (fixed columns, `columns` hint), rows (read straight
        across the page: keeps table rows together), boxes (pdfminer text boxes)."""
        spec = hints.get("layout", "columns")
        if isinstance(spec, list):
            return next((r["layout"] for r in spec if r["pages"][0] <= page_no <= r["pages"][1]), "columns")
        return spec

    def page_lines(page, page_no: int, columns: int) -> list[Line]:
        if columns == 1:
            return to_lines(page.extract_text_lines(return_chars=True), page_no, 0)
        # Full-width lines (an intro paragraph across both columns) are read
        # whole; the stretches between them are read column by column, so
        # reading order stays top to bottom.
        out: list[Line] = []
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
                    out += to_lines(region.extract_text_lines(return_chars=True), page_no, col)
            if band:
                out += to_lines([band], page_no, FULL_WIDTH)
                y = max(y, band["bottom"])
        return out

    by_page: dict[int, list[Line]] = {}
    with pdfplumber.open(pdf_path) as pdf:
        wanted = [p for p in pages if p <= len(pdf.pages)]
        box_pages = [p for p in wanted if layout_for(p) == "boxes"]
        for page_no in wanted:
            if page_no in box_pages:
                continue
            page = pdf.pages[page_no - 1].dedupe_chars().filter(keep)
            if layout_for(page_no) == "table":
                by_page[page_no] = table_lines(page, page_no)
            else:
                by_page[page_no] = page_lines(page, page_no, 1 if layout_for(page_no) == "rows" else columns)
    for page_no, box_no, raw in _box_lines(pdf_path, box_pages, keep_char):
        by_page.setdefault(page_no, []).extend(to_lines([raw], page_no, box_no))
    return [ln for p in sorted(by_page) for ln in by_page[p]]


def _box_lines(pdf_path: str, pages: list[int], keep_char):
    """Free-form layouts (`layout: boxes`): let pdfminer group characters into
    text boxes, so side-by-side boxes (main text, sidebars, card text) stay
    apart, then yield each box's lines in reading order, as
    (page_no, box_no, line) with `line` shaped like a pdfplumber text line
    and holding only the characters `keep_char` accepts. `pages` must exist
    in the document."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTAnno, LTChar, LTTextBox

    if not pages:
        return
    pages = sorted(pages)
    for page_no, layout in zip(pages, extract_pages(pdf_path, page_numbers=[p - 1 for p in pages], laparams=LAParams())):
        h = layout.height
        for box_no, box in enumerate(b for b in layout if isinstance(b, LTTextBox)):
            for tl in box:
                chars, text, last = [], "", None
                for obj in tl:
                    if isinstance(obj, LTAnno):
                        if text and not text.endswith(" "):
                            text += " "
                        continue
                    if not isinstance(obj, LTChar):
                        continue
                    c = {"fontname": obj.fontname, "size": obj.size, "text": obj.get_text(),
                         "x0": obj.x0, "x1": obj.x1, "top": h - obj.y1, "bottom": h - obj.y0}
                    if not keep_char(c):
                        continue
                    key = (c["text"], round(c["x0"]), round(c["top"]))
                    if key == last:
                        continue  # overprinted duplicate glyph
                    last = key
                    chars.append(c)
                    text += c["text"]
                if chars:
                    yield page_no, box_no, {
                        "text": " ".join(text.split()), "chars": chars,
                        "x0": min(c["x0"] for c in chars), "x1": max(c["x1"] for c in chars),
                        "top": min(c["top"] for c in chars), "bottom": max(c["bottom"] for c in chars),
                    }


def build_sections(lines: list[Line], parts: list[dict] | None = None, preamble_heading: str = "Introduction") -> list[Section]:
    """Group lines into sections. `parts` (from the manifest) label each section's
    top-level division; a part begins at the section whose heading is its
    `first_heading` (the first part needs none)."""
    parts = parts or []
    part = parts[0]["name"] if parts else None
    sections: list[Section] = []
    current: Section | None = None
    for ln in lines:
        if ln.is_part:
            # A page title ("BIRD POWERS"): the sections after it sit under it.
            # The same title atop a later page is a running header: skip it.
            name = normalize_heading(ln.text)
            if name != part:
                part = name
                current = Section(heading=name, page_start=ln.page, heading_lines=[ln])
                sections.append(current)
            continue
        if ln.is_heading:
            title_only = current is not None and current.part is None and current.heading == part
            if current is not None and current.lines and normalize_heading(ln.text) == current.heading and ln.page != current.page_start:
                # Running header: the same title printed atop a later page
                # ("BIRD POWERS"). The current section continues.
                continue
            if current is not None and not current.lines and not title_only:
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
            elif ln.is_subheading:
                # A labelled step starts a subsection of the current section. The
                # label line stays in the text: "b) Settlement Requires: Brick, ..."
                # is itself the rule.
                current = Section(heading=current.heading, page_start=ln.page, part=current.part, subheading=" ".join(ln.text.split()))
                sections.append(current)
            current.lines.append(ln)
        # A part may start at a heading that wraps, so check after merging.
        for p in parts[1:]:
            if current is not None and current.heading.lower() == p.get("first_heading", "").lower():
                part = current.part = p["name"]
    return [s for s in sections if s.lines]
