"""Diagnostic: locate the NAPR crop annex tables inside the PDF.

For the back portion of the report it prints, per page, how many county rows a
table holds, the table shape, and any crop keyword found in the page text. This
pinpoints the Irish / Sweet Potato annex pages (and any non-crop pages sitting
between annexes) so the extractor's page mapping can be corrected precisely.

Run from the project root after the crosswalk exists:
    python check_napr.py

Author: Aboubacar HEMA
"""
from pathlib import Path

import pandas as pd

from kenyadb.crosswalk import norm
from kenyadb.napr import ANNEXES, _clean_table, find_pdf

_KEYWORDS = ("maize", "sorghum", "finger millet", "pearl millet", "millet",
             "dry bean", "bean", "cow pea", "cowpea", "green gram", "pigeon",
             "irish", "sweet", "ware", "potato", "cassava", "banana", "rice",
             "wheat", "annex")


def main() -> None:
    base = Path(".")
    xpath = base / "data" / "processed" / "crosswalk_admin.csv"
    if not xpath.exists():
        print("[check_napr] crosswalk not built yet - run the build first")
        return
    county_norms = set(pd.read_csv(xpath)["county_norm"]) - {""}

    pdf = find_pdf(base)
    if pdf is None:
        print("[check_napr] no NAPR PDF at data/raw/kilimostat/*.pdf")
        return

    try:
        import pdfplumber
    except ImportError:
        print("[check_napr] pdfplumber not installed")
        return

    print(f"[check_napr] {pdf.name}")
    print(f"[check_napr] TOC printed pages: {dict(ANNEXES)}")
    with pdfplumber.open(str(pdf)) as doc:
        n = len(doc.pages)
        start = max(0, int(n * 0.6))
        print(f"[check_napr] {n} pages; scanning {start + 1}..{n} "
              f"(showing pages with a county table or a potato keyword)\n")
        for i in range(start, n):
            page = doc.pages[i]
            text = (page.extract_text() or "").replace("\n", " ")
            low = text.lower()
            kw = [w for w in _KEYWORDS if w in low]
            best, shape = 0, ""
            for t in (page.extract_tables() or []):
                ct = _clean_table(t)
                col0 = [norm(str(r[0])) for r in ct[2:] if r and r[0]]
                ncc = sum(1 for c in col0 if c in county_norms)
                if ncc >= best:
                    best, shape = ncc, f"{len(ct)}x{len(ct[0]) if ct else 0}"
            potato = any(k in ("irish", "sweet", "ware", "potato") for k in kw)
            if best >= 5 or potato:
                flag = "  <== potato" if potato else ""
                print(f"  p{i + 1:>3}: county_rows={best:>2} table={shape:<8} "
                      f"kw={kw}{flag}\n        text: {text[:90].strip()}")


if __name__ == "__main__":
    main()
