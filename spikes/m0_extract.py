"""M0 spike: can we get clean, page-mapped text out of a real rulebook PDF?

Usage: uv run python spikes/m0_extract.py <pdf> [--page N]

Reports encryption/permissions, text per page, likely printed page numbers
(short numeric lines near the page edge), and the font sizes used, so we can
see whether headings are detectable from font size/weight.
"""

import argparse
import re
from collections import Counter

import pdfplumber


def line_records(page):
    """Group chars into lines, keeping font size, font name and position."""
    lines = []
    for line in page.extract_text_lines(return_chars=True):
        chars = line["chars"]
        sizes = Counter(round(c["size"], 1) for c in chars)
        fonts = Counter(c["fontname"] for c in chars)
        lines.append(
            {
                "text": line["text"],
                "size": sizes.most_common(1)[0][0],
                "font": fonts.most_common(1)[0][0],
                "top": round(line["top"]),
                "x0": round(line["x0"]),
            }
        )
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--page", type=int, help="dump every line of one page (1-based)")
    args = ap.parse_args()

    with pdfplumber.open(args.pdf) as pdf:
        doc = pdf.doc
        print(f"pages: {len(pdf.pages)}")
        print(f"encrypted: {bool(doc.encryption)}")
        print(f"metadata: { {k: v for k, v in pdf.metadata.items() if k in ('Title', 'Producer', 'Creator')} }")

        if args.page:
            page = pdf.pages[args.page - 1]
            for ln in line_records(page):
                print(f"  [{ln['size']:>5} {ln['font'][:28]:<28} y={ln['top']:>4} x={ln['x0']:>4}] {ln['text']}")
            return

        size_counts = Counter()
        for i, page in enumerate(pdf.pages, start=1):
            lines = line_records(page)
            nchars = sum(len(ln["text"]) for ln in lines)
            h = page.height
            edge_nums = [
                ln["text"].strip()
                for ln in lines
                if re.fullmatch(r"\d{1,3}", ln["text"].strip()) and (ln["top"] > h * 0.9 or ln["top"] < h * 0.08)
            ]
            for ln in lines:
                size_counts[(ln["size"], "Bold" in ln["font"] or "bold" in ln["font"])] += 1
            first = lines[0]["text"][:60] if lines else ""
            print(f"pdf p{i:>2}: {nchars:>5} chars, {len(lines):>3} lines, edge numbers={edge_nums}  first: {first!r}")

        print("\nfont size (bold?) -> line count:")
        for (size, bold), n in sorted(size_counts.items(), key=lambda kv: -kv[0][0]):
            print(f"  {size:>5} {'B' if bold else ' '}  {n}")


if __name__ == "__main__":
    main()
