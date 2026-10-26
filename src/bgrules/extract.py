"""Turn a rulebook PDF into sections of verbatim text with page locations.

Driven by the per-document `extraction` hints in the manifest (see M0 findings):
pages are split into columns before line extraction, only heading/body fonts
are kept, and a section runs from one heading to the next, across columns and
pages. Long sections are split into passages so search results stay specific.
"""

import re
from collections import Counter
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
    ocr: bool = False  # read from a page image by OCR, not from the PDF's text


@dataclass
class Passage:
    lines: list[Line]
    heading_lines: list[Line] = field(default_factory=list)  # highlighted, not part of the text
    join_words: frozenset[str] = frozenset()  # see join_lines

    @property
    def page(self) -> int:
        return self.lines[0].page

    @property
    def text(self) -> str:
        return join_lines([ln.text for ln in self.lines], self.join_words)

    @property
    def spans(self) -> list[dict]:
        return spans(self.heading_lines + self.lines)

    @property
    def line_boxes(self) -> list[dict]:
        """Each line of the text with its page and box, for underlining a quote."""
        return [{"page": ln.page, "bbox": [round(v, 1) for v in ln.bbox], "text": ln.text, **({"ocr": True} if ln.ocr else {})}
                for ln in self.lines]


@dataclass
class Section:
    heading: str
    page_start: int
    part: str | None = None  # top-level division, e.g. "Game Rules" or "Almanac"
    lines: list[Line] = field(default_factory=list)
    heading_lines: list[Line] = field(default_factory=list)
    subheading: str | None = None  # set for a subsection: its label line, e.g. "b) Settlement Requires: ..."
    join_words: frozenset[str] = frozenset()  # see join_lines

    @property
    def path(self) -> list[str]:
        path = [self.part, self.heading] if self.part else [self.heading]
        return path + [self.subheading] if self.subheading else path

    @property
    def page_end(self) -> int:
        return self.lines[-1].page if self.lines else self.page_start

    @property
    def text(self) -> str:
        return join_lines([ln.text for ln in self.lines], self.join_words)

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
                out.append(Passage(current, join_words=self.join_words))
                current, size = [], 0
        if current:
            # Fold a short tail into the previous passage rather than leave a fragment.
            if out and size < target_chars / 3:
                out[-1].lines.extend(current)
            else:
                out.append(Passage(current, join_words=self.join_words))
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


def join_lines(lines: list[str], join_words: frozenset[str] = frozenset()) -> str:
    """Join wrapped lines into running text, dropping the stray space a dropped
    icon leaves before punctuation.

    A line ending in a hyphen keeps it ("award-" + "winning" -> "award-winning"):
    rulebooks rarely hyphenate at line breaks, and most such hyphens are real
    compounds. The word is joined without the hyphen only when the rulebook
    spells the joined form elsewhere (`join_words`, e.g. "counterclockwise").
    A spaced dash ("in nature -") is punctuation and always stays."""
    out = ""
    for text in lines:
        text = text.strip()
        if not text:
            continue
        if out.endswith("-") and not out.endswith(" -") and text[:1].islower():
            head = re.search(r"[A-Za-z]+-$", out)
            tail = re.match(r"[a-z]+", text)
            joined = head and tail and (head.group(0)[:-1] + tail.group(0)).lower() in join_words
            out = out[:-1] + text if joined else out + text
        elif out:
            out += " " + text
        else:
            out = text
    return re.sub(r" +([.,;:!?])", r"\1", out)


def normalize_heading(text: str) -> str:
    """Small-caps fonts extract as mixed case ("lonGest road"); title-case them.
    Ornaments standing alone around a title ("• Game Setup •") are dropped."""
    small = {"a", "an", "and", "of", "the", "to", "in", "on", "or", "for"}
    text = re.sub(r"(?:^|(?<=\s))[•·◆◇★☆✦❖]+(?=\s|$)", " ", text)

    def fix(m: re.Match) -> str:
        word = m.group(0)
        if m.start() > 0 and word.lower() in small:
            return word.lower()
        return word.capitalize()

    return re.sub(r"[A-Za-z]+(?:['’][A-Za-z]+)*", fix, " ".join(text.split()))  # "it’s" is one word


FULL_WIDTH = -1  # Line.column for text that spans the page's columns
GUTTER_MIN_GAP = 12.0  # points: a column gutter is wider than any word space


def _spaced_by_gaps(chars: list[dict]) -> str:
    """Text of a line rebuilt from its characters, with a space only where there
    is a visible gap. Small-caps titles print each word's first letter larger,
    and layout analysis puts a space at every size change ("Bef ore Your
    First Gam e")."""
    glyphs = sorted((c for c in chars if c["text"].strip()), key=lambda c: c["x0"])
    out = ""
    for a, b in zip([None, *glyphs], glyphs):
        if a is not None and b["x0"] - a["x1"] > 0.12 * min(a["size"], b["size"]):
            out += " "
        out += b["text"]
    return out


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


def default_hints(pdf_path: str) -> dict:
    """Extraction hints for a rulebook nobody has tuned (one a user added):
    body text is the most common font size; headings are lines set clearly
    larger, or in bold at body size, that read as titles (heading_max_words).
    Pdfminer's text boxes keep sidebars apart, with a wide line margin so
    widely spaced lines still form one box. Measured on the bundled rulebooks
    with their own hints removed: 80% hit@5 on their eval sets, against 86%
    with tuned hints (41% with no hints at all). Two-page spreads are found
    by _spread_hints."""
    sizes: dict[float, int] = {}
    with pdfplumber.open(pdf_path) as pdf:
        n_pages = len(pdf.pages)
        spreads = _spread_hints(pdf.pages)
        for page in pdf.pages:
            for c in page.chars:
                if c["text"].strip():
                    size = round(c["size"] * 2) / 2
                    sizes[size] = sizes.get(size, 0) + 1
    body = max(sizes, key=sizes.get) if sizes else 10.0
    hints = {
        "layout": "boxes",
        "line_margin": 1.0,
        "content_pages": [1, n_pages],
        "heading_fonts": [{"name": "", "min_size": round(body * 1.15, 2)}, {"name": "Bold", "min_size": round(body - 0.3, 2)}],
        "heading_max_words": 12,
    }
    return {**hints, **spreads}


def _spread_hints(pages) -> dict:
    """`spreads` (and `printed_page_offset`) for a PDF of a printed booklet whose
    inside pages are stored as spreads: a run of pages twice as wide as the
    narrowest page and as tall (a smaller card or insert is shorter, and not
    half a spread). The page numbers printed in the first spread's bottom
    corners say where the count starts, e.g. 2|3 after a one-page cover, or
    when there is no cover at all. A file of spreads only (every page the same
    landscape size) has nothing narrower to compare with, so there the printed
    numbers decide: n|n+1 on at least half the pages."""
    single = min(pages, key=lambda p: p.width)
    wide = [n for n, p in enumerate(pages, 1)
            if 1.8 <= p.width / single.width <= 2.2 and abs(p.height - single.height) <= 0.02 * single.height]
    same_size = all(abs(p.width - single.width) <= 0.02 * single.width
                    and abs(p.height - single.height) <= 0.02 * single.height for p in pages)
    if not wide and same_size and single.width > single.height:
        if sum(_spread_numbers(p) is not None for p in pages) >= max(2, len(pages) / 2):
            wide = list(range(1, len(pages) + 1))
    if not wide or wide != list(range(wide[0], wide[-1] + 1)):
        return {}
    hints = {"spreads": [wide[0], wide[-1]]}
    # Without an offset, the left half of spread n is printed page 2n - first;
    # take the offset most numbered spreads agree on.
    offsets = Counter(left - (2 * n - wide[0]) for n in wide if (left := _spread_numbers(pages[n - 1])) is not None)
    if offsets and (offset := offsets.most_common(1)[0][0]):
        hints["printed_page_offset"] = offset
    return hints


def _spread_numbers(page) -> int | None:
    """The left page number of a spread that prints n|n+1 along its bottom edge,
    placed as page numbers are: each well into its own half and mirroring the
    other (so not a score track or diagram labels running across the middle)."""
    w = page.width
    numbers = [x for x in page.extract_words()
               if x["text"].isdigit() and len(x["text"]) <= 3 and x["top"] > 0.9 * page.height]
    for a in numbers:
        for b in numbers:
            if (int(b["text"]) == int(a["text"]) + 1 and a["x1"] < 0.4 * w and b["x0"] > 0.6 * w
                    and abs(a["x0"] - (w - b["x1"])) <= 0.05 * w):
                return int(a["text"])
    return None


def extract_lines(pdf_path: str, hints: dict) -> list[Line]:
    columns = hints.get("columns", 1)
    heading_fonts = hints.get("heading_fonts", [])
    body_fonts = hints.get("body_fonts", [])
    body_min, body_max = hints.get("body_size", [0, 1_000])
    sub_rules = hints.get("subheadings", [])
    if isinstance(sub_rules, dict):
        sub_rules = [sub_rules]
    first, last = hints.get("content_pages", [1, 10_000])
    skip = set(hints.get("skip_pages", []))  # non-rules pages inside the range (lore, contents)
    run_in = hints.get("run_in_headings", False)  # headings that begin a line of body text
    pages = [p for p in range(first, last + 1) if p not in skip]

    def in_size(c, rule) -> bool:
        return rule.get("min_size", 0) - 0.1 <= c["size"] <= rule.get("max_size", 1_000) + 0.1

    def is_heading_char(c) -> bool:
        return any(h["name"] in c["fontname"] and in_size(c, h) for h in heading_fonts)

    def uppercase_only(c) -> bool:
        return any(h.get("uppercase") and h["name"] in c["fontname"] and in_size(c, h) for h in heading_fonts)

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
            # A table row (see _merge_tables) is never a heading, even in bold.
            heading = not ln.get("table") and sum(is_heading_char(c) for c in chars) > len(chars) / 2
            if heading and any(uppercase_only(c) for c in chars) and any(ch.islower() for ch in ln["text"]):
                # An `uppercase: true` heading font in normal case is flavor
                # text, not a heading: keep it as ordinary text.
                heading = False
            if heading and len({round(c["size"]) for c in chars if c["text"].strip()}) > 1:
                ln = {**ln, "text": _spaced_by_gaps(chars)}  # small caps: see _spaced_by_gaps
            if heading and not re.search(r"[A-Za-z]", ln["text"]):
                continue  # heading-font symbols in diagrams ("=", "+")
            if re.fullmatch(r"[A-Z]|\d{1,3}", ln["text"].strip()):
                continue  # almanac divider letters; page numbers
            subheading = not heading and not ln.get("table") and is_subheading(ln)
            part = heading and sum(any(h.get("part") and h["name"] in c["fontname"] and in_size(c, h) for h in heading_fonts) for c in chars) > len(chars) / 2
            if subheading:
                # A subheading whose sentence starts on the same line ("Spending
                # Money Any time you spend ..."): its own words title the
                # subsection, and the rest of the line follows as body text.
                in_rule = [i for i, c in enumerate(chars) if is_subheading({"text": ln["text"], "chars": [c]})]
                tail = chars[in_rule[-1] + 1:] if in_rule else []
                if re.search(r"[A-Za-z]", "".join(c["text"] for c in tail)):
                    head = chars[: in_rule[-1] + 1]
                    title = _leading_text(ln["text"], head)
                    for part_chars, text, is_sub in ((head, title, True), (tail, ln["text"][len(title):].strip(), False)):
                        out.append(Line(text=text, page=page_no, column=column, is_heading=False, is_subheading=is_sub,
                                        bbox=(min(c["x0"] for c in part_chars), min(c["top"] for c in part_chars),
                                              max(c["x1"] for c in part_chars), max(c["bottom"] for c in part_chars))))
                    continue
            if run_in and not heading and not subheading and not ln.get("table"):
                # A run-in heading starts the line in a heading font and the text
                # continues on the same line ("Ally cards appear only in ..."):
                # the heading's words become the section title, and the whole
                # line stays in the body so the text remains verbatim.
                lead = next((i for i, c in enumerate(chars) if not is_heading_char(c)), len(chars))
                visible = sum(bool(c["text"].strip()) for c in chars[:lead])  # space glyphs aside
                if lead >= 2 and lead < len(chars) and visible:
                    title = _leading_text(ln["text"], chars[:lead])
                    if re.search(r"[A-Za-z]{2}", title):
                        head = chars[:lead]
                        is_part = sum(any(h.get("part") and h["name"] in c["fontname"] and in_size(c, h) for h in heading_fonts) for c in head) > lead / 2
                        out.append(Line(text=title, page=page_no, column=column,
                                        bbox=(min(c["x0"] for c in head), min(c["top"] for c in head), max(c["x1"] for c in head), max(c["bottom"] for c in head)),
                                        is_heading=True, is_part=is_part))
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
        title_cols = set(hints["table"].get("title_columns", []))
        titles: dict[int, list[tuple[int, int, dict]]] = {}
        for r, ws in cells:
            raw = {"text": " ".join(w["text"] for w in ws), "chars": ws,
                   "x0": min(w["x0"] for w in ws), "x1": max(w["x1"] for w in ws),
                   "top": min(w["top"] for w in ws), "bottom": max(w["bottom"] for w in ws)}
            col = sum(raw["x0"] >= b - 4 for b in bounds)
            wide = col == 0 and raw["x1"] > bounds[0] + 20
            band = sum(s <= raw["top"] for s in seps)
            if col in title_cols and not wide:
                # `title_columns`: these cells (a card's number, name, points)
                # become the record's subsection title, e.g. "3 A Furrier 1".
                titles.setdefault(band, []).append((col, r, raw))
                continue
            for ln in to_lines([raw], page_no, col):
                lead = wide or ln.is_heading or ln.is_subheading
                key = (r, raw["x0"]) if lead else (col, r, raw["x0"])
                bands.setdefault(band, []).append((0 if lead else 1, key, ln))
        for band, parts in titles.items():
            parts.sort(key=lambda p: (p[0], p[1], p[2]["x0"]))
            raws = [p[2] for p in parts]
            title = Line(text=join_lines([p["text"] for p in raws]), page=page_no, column=0,
                         bbox=(min(p["x0"] for p in raws), min(p["top"] for p in raws), max(p["x1"] for p in raws), max(p["bottom"] for p in raws)),
                         is_heading=False, is_subheading=True)
            # Kind -1: the title heads its whole record, even though it sits
            # mid-height beside a description that starts above it.
            bands.setdefault(band, []).append((-1, (-1, 0.0), title))
        out: list[Line] = []
        for band in sorted(bands):
            items = bands[band]
            # A heading or intro line mid-band starts a new piece: cells above
            # it (the end of the previous record) stay before it.
            lead_rows = sorted(key[0] for kind, key, _ in items if kind == 0)
            def piece(kind, key):
                row = key[0] if kind <= 0 else key[1]
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
        # Pages with no text layer (scans, photos, "print to PDF" copies) are
        # read by OCR; so is every page of `layout: ocr`.
        ocr_pages = [p for p in wanted if layout_for(p) == "ocr" or not pdf.pages[p - 1].chars]
        box_pages = [p for p in wanted if layout_for(p) == "boxes" and p not in ocr_pages]
        for page_no in wanted:
            if page_no in box_pages or page_no in ocr_pages:
                continue
            page = pdf.pages[page_no - 1]
            # Text positioned outside the page (layout spill-over from a facing
            # page) is invisible in print: never index it.
            page = page.within_bbox(page.bbox).dedupe_chars().filter(keep)
            if layout_for(page_no) == "table":
                by_page[page_no] = table_lines(page, page_no)
            else:
                by_page[page_no] = page_lines(page, page_no, 1 if layout_for(page_no) == "rows" else columns)
    for page_no, box_no, raw in _box_lines(pdf_path, box_pages, keep_char, figure_text=hints.get("figure_text", False),
                                           line_margin=hints.get("line_margin", 0.5),
                                           inline_boxes=hints.get("inline_boxes", False)):
        by_page.setdefault(page_no, []).extend(to_lines([raw], page_no, box_no))
    for page_no, lines in _ocr_lines(pdf_path, ocr_pages, hints.get("ocr", {})):
        by_page[page_no] = lines
    _add_side_titles(by_page, hints.get("side_titles", {}))
    if hints.get("heading_max_words"):
        for lines in by_page.values():
            _demote_long_headings(lines, hints["heading_max_words"])
    skip = [re.compile(p) for p in hints.get("skip_text", [])]  # lines to drop: "VILLAIN GUIDE"
    # `page_order`: reading order for PDFs stored in print-imposition order
    # (a folded booklet whose cover is PDF page 3); unlisted pages follow.
    order = hints.get("page_order", [])
    ranked = sorted(by_page, key=lambda p: (order.index(p) if p in order else len(order), p))
    return [ln for p in ranked for ln in by_page[p] if not any(rx.fullmatch(ln.text.strip()) for rx in skip)]


OCR_SCALES = (3, 4)  # render scales (216 and 288 dpi): each region is read at both and merged


def _add_side_titles(by_page: dict[int, list[Line]], titles: dict[int, str]) -> None:
    """`side_titles`: page titles printed sideways down the margin, one letter
    per line, which line extraction can't read ({pdf page: title}). Each
    starts a part at the top of its page, as a printed page title would."""
    for page_no, title in titles.items():
        if by_page.get(page_no):
            first = by_page[page_no][0]
            x0, top = first.bbox[:2]
            by_page[page_no].insert(0, Line(text=title, page=page_no, column=first.column,
                                            bbox=(x0, top, x0, top), is_heading=True, is_part=True))


def _demote_long_headings(lines: list[Line], max_words: int) -> None:
    """`heading_max_words`: a run of heading lines, one wrapped under the other,
    holding more words than this is a paragraph set large (an intro or a
    sidebar), not a title: its lines become body text. So is a run that reads
    as part of a sentence: all lowercase ("in the same phase.", words in bold
    mid-sentence that wrapped onto their own line) or ending in a full stop.
    (Small caps extract as mixed case, "the solo Game", and stay headings.)"""
    i = 0
    while i < len(lines):
        if not lines[i].is_heading or lines[i].is_part:
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j].is_heading and not lines[j].is_part and _continues(lines[i:j], lines[j]):
            j += 1
        text = " ".join(ln.text.strip() for ln in lines[i:j])
        sentence = not re.search(r"[A-Z]", text) or text.endswith((".", ","))
        if len(text.split()) > max_words or sentence:
            for ln in lines[i:j]:
                ln.is_heading = False
        i = j


def _overlap(a: tuple, b: tuple) -> float:
    """Intersection over the smaller box's area."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    return w * h / min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))


def _ocr_lines(pdf_path: str, pages: list[int], rules: dict):
    """Read image-only pages with OCR, yielding (page_no, lines) in reading order.

    Uses the macOS Vision framework. Text detection can skip a line beside a
    picture at one resolution and find it at another, so each region is read
    at every scale in OCR_SCALES and the results are merged. `columns` splits
    the page into that many vertical strips read separately (panelled layouts
    read most reliably one panel at a time); each strip is a column for
    highlighting. OCR gives no fonts, so headings are told apart by line
    height (PDF points): `heading_min_height` (default 1.4x the median line
    height); `min_height` drops smaller lines, such as card text in pictures;
    lines below `min_confidence` (default 0.6: decorative borders read as
    "=---") are dropped too. `not_headings` lists patterns for tall lines that
    aren't headings, such as callouts set in a bigger font ("Note: ...").
    Lines whose characters average under `min_char_width` (default 0.3) of
    the median line height wide are dropped: real text is about 0.4, while
    small print inside pictures (card text) comes back squeezed into narrow
    boxes. (Box heights vary too much line to line to measure against.)"""
    if not pages:
        return
    try:
        import io

        import pypdfium2 as pdfium
        import Vision
        from Foundation import NSData
    except ImportError as e:
        raise RuntimeError(
            f"Pages {pages} have no text, only images, so they need OCR, which uses macOS's Vision framework ({e})."
        ) from e

    def recognise(image) -> list:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(True)
        Vision.VNImageRequestHandler.alloc().initWithData_options_(
            NSData.dataWithBytes_length_(buf.getvalue(), len(buf.getvalue())), None).performRequests_error_([request], None)
        return list(request.results() or [])

    n_cols = rules.get("columns", 1)
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        read: list[tuple[int, list[tuple[str, tuple[float, float, float, float], int]]]] = []
        for page_no in pages:
            page = pdf[page_no - 1]
            width, height = page.get_size()
            found: list[tuple[str, tuple[float, float, float, float], int, float]] = []
            for scale in OCR_SCALES:
                image = page.render(scale=scale).to_pil()
                for col in range(n_cols):
                    left, right = width * col / n_cols, width * (col + 1) / n_cols
                    strip = image.crop((round(left * scale), 0, round(right * scale), image.height))
                    for obs in recognise(strip):
                        best = obs.topCandidates_(1)[0]
                        if best.confidence() < rules.get("min_confidence", 0.6) or not re.search(r"[A-Za-z]", best.string()):
                            continue
                        b = obs.boundingBox()  # normalised to the strip, origin bottom-left
                        sw = right - left
                        bbox = (left + b.origin.x * sw, (1 - b.origin.y - b.size.height) * height,
                                left + (b.origin.x + b.size.width) * sw, (1 - b.origin.y) * height)
                        same = next((i for i, f in enumerate(found) if _overlap(f[1], bbox) > 0.5), None)
                        if same is None:
                            found.append((best.string(), bbox, col, best.confidence()))
                        elif best.confidence() > found[same][3]:
                            found[same] = (best.string(), bbox, col, best.confidence())
            found.sort(key=lambda f: (f[2], f[1][1]))  # by strip, then top to bottom
            read.append((page_no, [(t, bb, c) for t, bb, c, _ in found]))
    finally:
        pdf.close()

    heights = sorted(bb[3] - bb[1] for _, found in read for _, bb, _ in found)
    median = heights[len(heights) // 2] if heights else 0.0
    heading_min = rules.get("heading_min_height", 1.4 * median)
    not_headings = [re.compile(p) for p in rules.get("not_headings", [])]
    for page_no, found in read:
        lines, column, last = [], 0, None
        for text, bbox, strip in found:
            h = bbox[3] - bbox[1]
            if h < rules.get("min_height", 0):
                continue
            if (bbox[2] - bbox[0]) / max(len(text), 1) < rules.get("min_char_width", 0.3) * median:
                continue  # squeezed into a tiny box: small print in a picture (a card), read at a stretch
            if last is not None and (strip != last[0] or bbox[1] < last[1] - 2):
                column += 1  # a new strip, or back up the page: a new column
            last = (strip, bbox[1])
            heading = h >= heading_min and not any(rx.match(text) for rx in not_headings)
            if heading:
                text = re.sub(r"^[^\w(“\"]+", "", text)  # an icon read as a symbol: "® Goal: Win Big"
            lines.append(Line(text=text, page=page_no, column=column, bbox=bbox, is_heading=heading, ocr=True))
        yield page_no, lines


def _box_lines(pdf_path: str, pages: list[int], keep_char, figure_text: bool = False, line_margin: float = 0.5,
               inline_boxes: bool = False):
    """Free-form layouts (`layout: boxes`): let pdfminer group characters into
    text boxes, so side-by-side boxes (main text, sidebars, card text) stay
    apart, then yield each box's lines in reading order, as
    (page_no, box_no, line) with `line` shaped like a pdfplumber text line
    and holding only the characters `keep_char` accepts. `pages` must exist
    in the document. `figure_text` also reads text drawn inside embedded
    graphics (some rulebooks set their page titles that way). `line_margin`
    is how far apart (in line heights) lines may be and still form one text
    box: raise it when widely spaced lines each become their own box, which
    then get read row by row across the columns. `inline_boxes` merges words
    that pdfminer split off a line (around inline icons) back into it."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTAnno, LTChar, LTFigure, LTTextBox

    def figure_boxes(container):
        for el in container:
            if isinstance(el, LTTextBox):
                yield el
            elif isinstance(el, LTFigure):
                yield from figure_boxes(el)

    def reading_order(layout) -> list:
        """The page's text boxes in pdfminer's reading order. Boxes from inside
        graphics come last in that order, so each is moved to just before the
        first box below it that it overlaps horizontally: a title drawn as a
        graphic then heads the text under it."""
        boxes = [b for b in layout if isinstance(b, LTTextBox)]
        if not figure_text:
            return boxes
        for fig in (b for el in layout if isinstance(el, LTFigure) for b in figure_boxes(el)):
            below = [i for i, b in enumerate(boxes) if b.y1 <= fig.y1 + 2 and b.x0 < fig.x1 and fig.x0 < b.x1]
            boxes.insert(below[0] if below else len(boxes), fig)
        return boxes

    def box_lines(box, layout) -> list[dict]:
        h, lines = layout.height, []
        for tl in box:
            chars, text, seen, runs = [], "", set(), []
            for obj in tl:
                if isinstance(obj, LTAnno):
                    if text and not text.endswith(" "):
                        text += " "
                    continue
                if not isinstance(obj, LTChar):
                    continue
                c = {"fontname": obj.fontname, "size": obj.size, "text": obj.get_text(),
                     "x0": obj.x0, "x1": obj.x1, "top": h - obj.y1, "bottom": h - obj.y0}
                if obj.x1 < 0 or obj.x0 > layout.width or obj.y1 < 0 or obj.y0 > h:
                    continue  # outside the page: invisible in print
                if not keep_char(c):
                    continue
                key = (c["text"], round(c["x0"]), round(c["top"]))
                if key in seen:
                    continue  # overprinted duplicate glyph (bold or outlined text drawn twice)
                seen.add(key)
                chars.append(c)
                if c["text"].isspace() or text.endswith(" ") or not runs:
                    runs.append([])
                if not c["text"].isspace():
                    runs[-1].append(c)
                text += c["text"]
            if chars:
                line = {
                    "text": " ".join(text.split()), "chars": chars, "runs": [r for r in runs if r],
                    "x0": min(c["x0"] for c in chars), "x1": max(c["x1"] for c in chars),
                    "top": min(c["top"] for c in chars), "bottom": max(c["bottom"] for c in chars),
                }
                # A box can split one row in two (a full stop after an inline icon).
                row = inline_boxes and not _is_label(line) and next((a for a in lines if _same_row(a, line, (a["x1"], a["x1"]))), None)
                if row:
                    _merge_into(row, line)
                else:
                    lines.append(line)
        return lines

    if not pages:
        return
    pages = sorted(pages)
    laparams = LAParams(all_texts=figure_text, line_margin=line_margin)
    for page_no, layout in zip(pages, extract_pages(pdf_path, page_numbers=[p - 1 for p in pages], laparams=laparams)):
        boxes = [box_lines(box, layout) for box in reading_order(layout)]
        if inline_boxes:
            boxes = _merge_inline(boxes)
        boxes = _merge_tables(boxes)
        for box_no, lines in enumerate(boxes):
            for line in lines:
                del line["runs"]
                yield page_no, box_no, line


TABLE_MIN_ROWS = 3  # a header and two rows, or three rows
TABLE_CELL_MAX_WORDS = 8


def _merge_tables(boxes: list[list[dict]]) -> list[list[dict]]:
    """A small table set as loose text puts every cell in a one-line box of its
    own ("Number of players", "2", "4 Skill tokens"), which would read as a
    list of fragments: bold column headers taken for headings, lone numbers
    dropped as labels. Find one-line boxes in near-body-size text that line up
    in columns, two or more cells a row, each row close under the last, for at
    least TABLE_MIN_ROWS rows; merge them into one box, a line per row written
    as a Markdown table row ("| 2 | 4 Skill tokens |", so rows stay apart in
    running text), placed where the table's first cell was in reading
    order. The first row's columns are kept only where the second row has a
    cell under them, so a stray box on the header's row (a diagram callout)
    is left out; after that, every row fills every column."""
    def size(ln):
        return max(c["size"] for c in ln["chars"])
    sizes = sorted(size(ln) for b in boxes for ln in b)
    if not sizes:
        return boxes
    typical = sizes[len(sizes) // 2]
    cells = [i for i, b in enumerate(boxes)
             if len(b) == 1 and len(b[0]["text"].split()) <= TABLE_CELL_MAX_WORDS and size(b[0]) >= 0.8 * typical]
    line = {i: boxes[i][0] for i in cells}

    def same_row(i: int, j: int) -> bool:  # overlapping by half the shorter one's height
        a, b = line[i], line[j]
        overlap = min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])
        return overlap > 0.5 * min(a["bottom"] - a["top"], b["bottom"] - b["top"])

    rows: list[list[int]] = []  # cells sharing a row, left to right
    for i in sorted(cells, key=lambda i: line[i]["top"]):
        if rows and same_row(i, rows[-1][0]):
            rows[-1].append(i)
        else:
            rows.append([i])
    rows = [sorted(r, key=lambda i: line[i]["x0"]) for r in rows]

    def overlaps(a: tuple, i: int) -> bool:
        return a[0] < line[i]["x1"] and line[i]["x0"] < a[1]

    def follow(table: dict, r: list[int]) -> bool:
        """Add row `r` to `table` if it lines up under the table's columns."""
        under = [[i for i in r if overlaps(col, i)] for col in table["cols"]]
        keep = [k for k, m in enumerate(under) if len(m) == 1]
        if len(table["rows"]) > 1 and len(keep) < len(under) or len(keep) < 2:
            return False
        picked = [under[k][0] for k in keep]
        prev = [table["rows"][-1][k] for k in keep]
        height = max(line[i]["bottom"] - line[i]["top"] for i in prev)
        if len(set(picked)) < len(picked) or min(line[i]["top"] for i in picked) - max(line[i]["bottom"] for i in prev) > 2 * height:
            return False
        table["rows"] = [[row[k] for k in keep] for row in table["rows"]] + [picked]
        table["cols"] = [(min(table["cols"][k][0], line[i]["x0"]), max(table["cols"][k][1], line[i]["x1"])) for k, i in zip(keep, picked)]
        return True

    tables, table = [], None
    for r in rows:
        if table and follow(table, r):
            continue
        tables.append(table)
        apart = all(line[a]["x1"] < line[b]["x0"] for a, b in zip(r, r[1:]))
        table = {"cols": [(line[i]["x0"], line[i]["x1"]) for i in r], "rows": [r]} if len(r) >= 2 and apart else None
    tables.append(table)

    merged: dict[int, list[dict]] = {}  # first box of each table -> its row lines
    gone: set[int] = set()
    for table in tables:
        if not table or len(table["rows"]) < TABLE_MIN_ROWS:
            continue
        lines = []
        for r in table["rows"]:
            parts = [line[i] for i in r]
            lines.append({
                "text": "| " + " | ".join(ln["text"] for ln in parts) + " |", "chars": [c for ln in parts for c in ln["chars"]],
                "runs": [run for ln in parts for run in ln["runs"]], "table": True,
                "x0": min(ln["x0"] for ln in parts), "x1": max(ln["x1"] for ln in parts),
                "top": min(ln["top"] for ln in parts), "bottom": max(ln["bottom"] for ln in parts),
            })
        ids = [i for r in table["rows"] for i in r]
        merged[min(ids)] = lines
        gone.update(ids)
    return [merged.get(i, b) for i, b in enumerate(boxes) if i in merged or i not in gone]


def _is_label(line: dict) -> bool:
    """A lone capital or number: a diagram callout or page number, which
    is dropped later and must not be merged into a sentence."""
    return bool(re.fullmatch(r"[A-Z]|\d{1,3}", line["text"]))


def _same_row(a: dict, b: dict, column: tuple[float, float]) -> bool:
    """True if line `b` sits on line `a`'s row, either within the column
    (left and right edges) of `a`'s box or a few letters from `a`."""
    overlap = min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])
    size = max(c["size"] for c in b["chars"])
    gap = max(b["x0"] - a["x1"], a["x0"] - b["x1"])
    inside = column[0] - 2 <= b["x0"] and b["x1"] <= column[1] + 2
    return overlap > (b["bottom"] - b["top"]) / 2 and (inside or gap < 1.5 * size)


def _merge_into(a: dict, b: dict) -> None:
    """Merge line `b`'s words into line `a`, left to right, dropping
    overprinted duplicates of glyphs `a` already has."""
    seen = {(c["text"], round(c["x0"]), round(c["top"])) for c in a["chars"]}
    fresh = [[c for c in r if (c["text"], round(c["x0"]), round(c["top"])) not in seen] for r in b["runs"]]
    b = {**b, "runs": [r for r in fresh if r], "chars": [c for r in fresh for c in r]}
    if not b["chars"]:
        return
    runs = sorted(a["runs"] + b["runs"], key=lambda r: r[0]["x0"])
    text = "".join(c["text"] for c in runs[0])
    for prev, run in zip(runs, runs[1:]):
        gap = run[0]["x0"] - prev[-1]["x1"]
        text += (" " if gap > 0.1 * run[0]["size"] else "") + "".join(c["text"] for c in run)
    a.update(text=" ".join(text.split()), chars=sorted(a["chars"] + b["chars"], key=lambda c: c["x0"]), runs=runs,
             x0=min(a["x0"], b["x0"]), x1=max(a["x1"], b["x1"]),
             top=min(a["top"], b["top"]), bottom=max(a["bottom"], b["bottom"]))


def _merge_inline(boxes: list[list[dict]]) -> list[list[dict]]:
    """Words set in another style in mid-sentence (beside an inline icon,
    say) can come out as their own small text box over the paragraph's
    box. Merge such a box back into the bigger box whose lines it sits on,
    so the sentence reads in order."""
    merged, changed = set(), True
    while changed:  # a merge can bring the next fragment within reach
        changed = False
        for j in sorted(set(range(len(boxes))) - merged, key=lambda j: len(boxes[j])):
            for i, host in enumerate(boxes):
                if i == j or i in merged or len(host) <= len(boxes[j]) or any(map(_is_label, boxes[j])):
                    continue
                column = (min(a["x0"] for a in host), max(a["x1"] for a in host))
                rows = [next((a for a in host if _same_row(a, b, column)), None) for b in boxes[j]]
                if all(rows):
                    for a, b in zip(rows, boxes[j]):
                        _merge_into(a, b)
                    merged.add(j)
                    changed = True
                    break
    return [b for j, b in enumerate(boxes) if j not in merged]


def _leading_text(text: str, head: list[dict]) -> str:
    """The start of a line's `text` that holds the characters `head` (the
    line's first characters), space glyphs aside."""
    visible = sum(bool(c["text"].strip()) for c in head)
    seen, cut = 0, 0
    for cut, ch in enumerate(text):
        seen += ch != " "
        if seen == visible:
            break
    return text[: cut + 1].strip()


def _continues(heading_lines: list[Line], ln: Line) -> bool:
    """True if `ln` can be the next line of a wrapped heading: directly below
    its last line, not a separate heading beside it in another column."""
    if not heading_lines:
        return True
    prev = heading_lines[-1]
    height = prev.bbox[3] - prev.bbox[1]
    below = 0 <= ln.bbox[1] - prev.bbox[1] <= 2.5 * height
    return ln.page == prev.page and below and ln.bbox[0] < prev.bbox[2] and prev.bbox[0] < ln.bbox[2]


def _beside(heading_lines: list[Line], ln: Line) -> bool:
    """True if heading line `ln` sits to the right of a heading at the same height."""
    prev = heading_lines[0]
    return ln.page == prev.page and abs(ln.bbox[1] - prev.bbox[1]) < (prev.bbox[3] - prev.bbox[1]) / 2 and ln.bbox[0] >= heading_lines[-1].bbox[2]


def _column_home(sections: list[Section], current: Section | None, ln: Line) -> Section | None:
    """The section a body line continues when it sits wholly left of the current
    heading's column: the nearest earlier section on the page headed above it
    in its own column. None when the line belongs to the current section."""
    if current is None or not current.heading_lines or ln.is_subheading:
        return None
    head = current.heading_lines[0]
    if head.page != ln.page or ln.bbox[2] >= head.bbox[0] - 5:
        return None
    above = [s for s in sections if s is not current
             and not any(h.is_part for h in s.heading_lines)  # page titles head no text of their own
             and (s.heading_lines or s.lines) and (s.heading_lines or s.lines)[0].page == ln.page
             and (s.heading_lines or s.lines)[0].bbox[0] <= ln.bbox[0] + 10
             and (s.heading_lines or s.lines)[0].bbox[1] < ln.bbox[1]]
    return max(above, key=lambda s: (s.heading_lines or s.lines)[0].bbox[1], default=None)


def build_sections(lines: list[Line], parts: list[dict] | None = None, preamble_heading: str = "Introduction",
                   column_continuations: bool = False) -> list[Section]:
    """Group lines into sections. `parts` (from the manifest) label each section's
    top-level division; a part begins at the section whose heading is its
    `first_heading` (the first part needs none). `column_continuations` (hint)
    sends a line left of the current heading's column back to the section
    headed above it there (see _column_home)."""
    parts = parts or []
    part = parts[0]["name"] if parts else None
    # Words the rulebook spells whole: a line-break hyphen inside one of them is dropped.
    join_words = frozenset(w.lower() for ln in lines for w in re.findall(r"[A-Za-z]+", ln.text))
    sections: list[Section] = []
    current: Section | None = None
    # Headings side by side at the same height (one per column) can be read
    # before any of their text. `row` holds each column's left edge and its
    # latest section (the heading, or a subsection under it), so each following
    # line goes to the column it sits in.
    row: list[tuple[float, Section]] = []
    for ln in lines:
        if ln.is_part:
            # A page title ("BIRD POWERS"): the sections after it sit under it.
            # The same title atop a later page is a running header: skip it.
            name = normalize_heading(ln.text)
            if name != part:
                part = name
                current = Section(heading=name, page_start=ln.page, heading_lines=[ln])
                sections.append(current)
                row = []
            continue
        if ln.is_heading:
            title_only = current is not None and current.part is None and current.heading == part
            if current is not None and current.lines and normalize_heading(ln.text) == current.heading and ln.page != current.page_start:
                # Running header: the same title printed atop a later page
                # ("BIRD POWERS"). The current section continues.
                continue
            if current is not None and not current.lines and not title_only and _continues(current.heading_lines, ln):
                # Heading wrapped onto a second line: merge into the same heading.
                current.heading = f"{current.heading} {normalize_heading(ln.text)}"
                current.heading_lines.append(ln)
            else:
                beside = current is not None and not current.lines and any(sec is current for _, sec in row) and _beside(current.heading_lines, ln)
                current = Section(heading=normalize_heading(ln.text), page_start=ln.page, part=part, heading_lines=[ln])
                sections.append(current)
                row = [*row, (ln.bbox[0], current)] if beside else [(ln.bbox[0], current)]
        else:
            home = _column_home(sections, current, ln) if column_continuations else None
            if home is not None:
                # The rest of an earlier section, read after a heading in another
                # column ("Ending the Game" continued beside "A Word From ...").
                home.lines.append(ln)
                continue
            col = None
            if len(row) > 1 and any(sec is current for _, sec in row):
                col = max((i for i, (x, _) in enumerate(row) if x <= ln.bbox[0] + 10), key=lambda i: row[i][0], default=0)
                current = row[col][1]
            if current is None:
                current = Section(heading=preamble_heading, page_start=ln.page, part=part)
                sections.append(current)
            elif ln.is_subheading and current.subheading and all(l.is_subheading for l in current.lines):
                # A subheading wrapped onto another line ("FEAR EARNED THROUGH" /
                # "FEAR EFFECTS"): one subsection, one title.
                current.subheading = f"{current.subheading} {' '.join(ln.text.split())}"
            elif ln.is_subheading:
                # A labelled step starts a subsection of the current section. The
                # label line stays in the text: "b) Settlement Requires: Brick, ..."
                # is itself the rule.
                current = Section(heading=current.heading, page_start=ln.page, part=current.part, subheading=" ".join(ln.text.split()))
                sections.append(current)
                if col is not None:
                    row[col] = (row[col][0], current)
                else:
                    row = []
            current.lines.append(ln)
        # A part may start at a heading that wraps, so check after merging.
        for p in parts[1:]:
            if current is not None and current.heading.lower() == p.get("first_heading", "").lower():
                part = current.part = p["name"]
    for s in sections:
        s.join_words = join_words
    return [s for s in sections if s.lines]
