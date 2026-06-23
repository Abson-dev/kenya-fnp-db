"""National Agriculture Production Report (NAPR) 2024 extractor.

Parses the KNBS NAPR 2024 PDF (provided locally in data/raw/kilimostat/) into a
tidy county-level crop table. The report's Annexes give Area and Production by
county for ten crops over 2019-2023; those annex tables are printed in landscape
(rotated) with a two-tier header (year over Area / Production). This module
reuses the rotation fix and header-flattening that were validated by hand, then
joins each county to the master crosswalk and writes:

  data/processed/food/napr_crop_county.csv   tidy: county, crop, year, area, production
  data/processed/food/napr_provenance.json   checksum, pages, row counts (folded into the ledger)

The page numbers in ANNEXES are the printed pages from the report's table of
contents. PDF page = printed page + PAGE_OFFSET. The offset is auto-detected
from the Maize annex when possible, and falls back to the documented value.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .crosswalk import norm

# crop -> printed pages of its "Area and Production ... by County" annex (from the TOC)
ANNEXES = {
    "Maize": [97, 98],
    "Sorghum": [99],
    "Finger Millet": [100],
    "Pearl Millet": [101],
    "Dry Beans": [102],
    "Cow peas": [103],
    "Green grams": [104],
    "Pigeon peas": [105],
    "Irish Potatoes": [106],
    "Sweet Potatoes": [107],
}
PAGE_OFFSET_DEFAULT = 17          # PDF page = printed page + 17 (1-based)
_NUM = re.compile(r"^-?[\d,]+(?:\.\d+)?$")


def find_pdf(base: Path):
    d = Path(base) / "data" / "raw" / "kilimostat"
    if not d.exists():
        return None
    pdfs = sorted(d.rglob("*.pdf"))
    if not pdfs:
        return None
    pdfs.sort(key=lambda p: 0 if any(k in p.name.lower() for k in
              ("agriculture", "production", "napr")) else 1)
    return pdfs[0]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _num(v):
    if v is None:
        return None
    s = re.sub(r"[^0-9.\-]", "", str(v).replace(",", ""))
    if s in ("", "-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _clean_table(table):
    """Reorient a landscape (rotated) annex table so County is the first column
    and text reads normally. Detected by an unusually wide first row."""
    if not table or len(table) == 0:
        return table
    if len(table[0]) > 20:
        rev = [[c[::-1] if isinstance(c, str) else c for c in row] for row in table]
        trans = list(map(list, zip(*rev)))
        return [row[::-1] for row in trans]
    return table


def _parse_crop(rows, crop, county_norms):
    """Flatten the two-tier header (year over Area / Production) and melt the
    body into tidy records keyed on the county name and year."""
    if not rows or len(rows) < 3:
        return []
    year_row = [str(c or "").strip() for c in rows[0]]
    metric_row = [str(c or "").strip() for c in rows[1]]
    ncol = max(len(year_row), len(metric_row))

    spec, cur_year = [], None
    for i in range(ncol):
        h1 = year_row[i] if i < len(year_row) else ""
        h2 = (metric_row[i] if i < len(metric_row) else "").lower()
        ym = re.search(r"(19|20)\d{2}", h1) or re.search(r"(19|20)\d{2}", h2)
        if ym:
            cur_year = ym.group(0)
        if i == 0 or "county" in h1.lower() or "county" in h2:
            spec.append(("county", None))
        elif "area" in h2:
            spec.append(("area", cur_year))
        elif "prod" in h2:
            spec.append(("production", cur_year))
        else:
            spec.append((None, cur_year))

    recs = {}
    for row in rows[2:]:
        if not row or row[0] is None or not str(row[0]).strip():
            continue
        name = str(row[0]).replace("\n", " ").strip()
        low = name.lower()
        if low.startswith(("total", "source", "note", "annex")):
            continue
        if norm(name) not in county_norms:
            continue
        for i, (kind, year) in enumerate(spec):
            if kind in ("area", "production") and year and i < len(row):
                recs.setdefault((name, year), {})[kind] = _num(row[i])

    out = []
    for (name, year), d in recs.items():
        if d.get("area") is None and d.get("production") is None:
            continue
        out.append({"county_raw": name, "crop": crop, "year": int(year),
                    "area_ha": d.get("area"), "production_mt": d.get("production")})
    return out


def _page_records(doc, idx, crop, county_norms):
    """Parse a single PDF page (0-based index) into crop records, returning an
    empty list when the page holds no county table for this crop. Used by the
    forward-search fallback when a printed page has drifted."""
    if not (0 <= idx < len(doc.pages)):
        return []
    rows = []
    for t in (doc.pages[idx].extract_tables() or []):
        rows.extend(_clean_table(t))
    return _parse_crop(rows, crop, county_norms)


def _detect_offset(doc, county_norms, default: int) -> int:
    """Find the Maize annex (first full-county crop table in the back of the
    report) and derive PDF page = printed + offset; fall back to default."""
    start = int(len(doc.pages) * 0.55)
    for idx in range(start, len(doc.pages)):
        for t in (doc.pages[idx].extract_tables() or []):
            ct = _clean_table(t)
            col0 = [norm(str(r[0])) for r in ct[2:] if r and r[0]]
            if sum(1 for c in col0 if c in county_norms) >= 30:
                # idx is 0-based; printed Maize page is ANNEXES["Maize"][0]
                return (idx + 1) - ANNEXES["Maize"][0]
    return default


def run(base: Path, page_offset: int | None = None) -> dict:
    """Extract the NAPR annex crop tables into a tidy county-level CSV."""
    import pandas as pd

    base = Path(base)
    proc = base / "data" / "processed" / "food"
    proc.mkdir(parents=True, exist_ok=True)

    pdf = find_pdf(base)
    if pdf is None:
        print("[napr] no PDF at data/raw/kilimostat/*.pdf - skipping")
        return {}

    xwalk_path = base / "data" / "processed" / "crosswalk_admin.csv"
    if not xwalk_path.exists():
        print("[napr] crosswalk not built yet - run the crosswalk step first")
        return {}
    xwalk = pd.read_csv(xwalk_path)
    county_norms = set(xwalk["county_norm"]) - {""}

    try:
        import pdfplumber
    except ImportError:
        print("[napr] pdfplumber not installed - skipping")
        return {}

    records, per_crop = [], {}
    with pdfplumber.open(str(pdf)) as doc:
        npages = len(doc.pages)
        offset = page_offset if page_offset is not None else _detect_offset(
            doc, county_norms, PAGE_OFFSET_DEFAULT)
        # The printed-page + offset mapping holds for the early annexes, but a
        # stray page in the annex run can shift the later ones. So: try the
        # printed page first, and if it parses to nothing, scan forward from
        # just past the previous annex until the crop's table turns up.
        search_window = 8
        last_idx = -1
        for crop, printed in ANNEXES.items():
            rows = []
            for pp in printed:
                idx = pp + offset - 1            # 0-based PDF page index
                if idx <= last_idx or not (0 <= idx < npages):
                    continue                     # skip pages already consumed / out of range
                for t in (doc.pages[idx].extract_tables() or []):
                    rows.extend(_clean_table(t))
            recs = _parse_crop(rows, crop, county_norms) if rows else []
            end_idx = printed[-1] + offset - 1
            if not recs:
                start = max(printed[0] + offset - 1, last_idx + 1)
                for idx in range(start, min(start + search_window, npages)):
                    cand = _page_records(doc, idx, crop, county_norms)
                    if cand:
                        recs, end_idx = cand, idx
                        print(f"[napr] {crop}: recovered at PDF page {idx + 1} "
                              f"(printed {printed[0]}, expected {printed[0] + offset})")
                        break
            if recs:
                last_idx = max(last_idx, end_idx)
            per_crop[crop] = len(recs)
            records.extend(recs)

    if not records:
        print(f"[napr] {pdf.name}: no annex rows parsed (offset={offset}). "
              "Check the page offset or that the annex pages are present.")
        return {}

    df = pd.DataFrame(records)
    df["county_norm"] = df["county_raw"].map(norm)
    cmap = xwalk.drop_duplicates("county_norm").set_index("county_norm")[["county_code", "county_name"]]
    df = df.join(cmap, on="county_norm")
    df = df[df["county_code"].notna()]
    df = (df[["county_code", "county_name", "county_norm", "crop", "year",
              "area_ha", "production_mt"]]
          .sort_values(["crop", "year", "county_name"]).reset_index(drop=True))
    out = proc / "napr_crop_county.csv"
    df.to_csv(out, index=False)

    n_county = df["county_name"].nunique()
    n_crop = df["crop"].nunique()
    yr = f"{int(df['year'].min())}-{int(df['year'].max())}"
    print(f"[napr] {npages} pages (offset {offset}) -> {len(df)} rows, "
          f"{n_crop} crops, {n_county} counties, {yr} -> {out.name}")

    manifest = {
        "source_key": "kilimostat",
        "title": "Kenya National Agriculture Production Report 2024 (crop area and production by county)",
        "publisher": "KNBS / Ministry of Agriculture and Livestock Development",
        "layer": "food",
        "access": "open_download (local)",
        "pdf_path": str(pdf),
        "sha256": _sha256(pdf),
        "bytes": pdf.stat().st_size,
        "pages": npages,
        "page_offset": offset,
        "rows": len(df),
        "crops": n_crop,
        "counties": n_county,
        "years": yr,
        "rows_per_crop": per_crop,
        "message": (f"local extract: {len(df)} rows, {n_crop} crops x {n_county} "
                    f"counties, {yr}"),
        "extracted_by": "Aboubacar HEMA",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    (proc / "napr_provenance.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"napr_crop_county": str(out)}
