"""M4 spike: can pdfminer's layout analysis untangle free-form pages (Wingspan)?

Usage: uv run python spikes/m4_boxes.py <pdf> <page> [<page> ...]

Prints each text box pdfminer finds, in its reading order, with the box
position and dominant font, so we can see whether side-by-side text boxes
(main text, sidebars, card text) come out separately.
"""

import sys
from collections import Counter

from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTChar, LTTextBox


def main():
    pdf, pages = sys.argv[1], [int(p) for p in sys.argv[2:]]
    for page_no, layout in enumerate(extract_pages(pdf, page_numbers=[p - 1 for p in pages], laparams=LAParams()), start=0):
        print(f"===== page {pages[page_no]}")
        for box in layout:
            if not isinstance(box, LTTextBox):
                continue
            chars = [c for line in box for c in line if isinstance(c, LTChar)]
            if not chars:
                continue
            font, size = Counter((c.fontname.split("+")[-1], round(c.size, 1)) for c in chars).most_common(1)[0][0]
            if size < 5:
                continue
            text = " ".join(box.get_text().split())
            print(f"[x{box.x0:>4.0f}-{box.x1:<4.0f} top{layout.height - box.y1:>4.0f}] {font[:20]:<20}{size:>5} | {text[:110]}")


if __name__ == "__main__":
    main()
