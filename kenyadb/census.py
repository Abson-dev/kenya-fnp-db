"""2019 Kenya Population and Housing Census: geography-layer denominators.

Two jobs, both run locally:

1. census_population(): read the KNBS published data tables (the direct XLSX
   files: population, households and density by county and by sub-county) into
   tidy geography tables joined to the crosswalk. These XLSX are the clean,
   machine-readable denominators and are the recommended source.

2. record_local_pdf(): record provenance (checksum, page count) and the
   extractable full text of the locally-provided census PDFs (Volume I and the
   Agriculture Analytical Report). Those PDFs are graphics-and-narrative reports
   whose tables are not reliably machine-readable, so they are kept for
   provenance and reference while the structured numbers come from the XLSX.

Outputs:
  data/processed/geography/census_population_county.csv
  data/processed/geography/census_population_subcounty.csv
  data/processed/geography/<source>_fulltext.txt
  data/processed/geography/<source>_provenance.json   (folded into the ledger)

Author: Aboubacar HEMA
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .crosswalk import norm


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _src_dirs(base: Path, source: str):
    return [Path(base) / "data" / sub / source for sub in ("raw", "external")]


def _find(base: Path, source: str, exts) -> list[Path]:
    out = []
    for d in _src_dirs(base, source):
        if d.exists():
            for p in sorted(d.rglob("*")):
                if p.suffix.lower() in exts:
                    out.append(p)
    return out


# --------------------------------------------------------------------------
# 1. KNBS XLSX denominators
# --------------------------------------------------------------------------
# header keyword -> output column. Matched case-insensitively against the
# detected header row of each KNBS sheet.
_COLMAP = [
    ("subcounty", ["sub-county", "subcounty", "sub county"]),
    ("county", ["county"]),
    ("population_total", ["total", "total population", "population"]),
    ("population_male", ["male"]),
    ("population_female", ["female"]),
    ("households", ["number of households", "households", "household"]),
    ("avg_household_size", ["average household size", "household size", "av. hh"]),
    ("land_area_sqkm", ["land area", "area"]),
    ("density", ["density", "population density"]),
]


def _detect_header(df_raw):
    """Find the row index that looks like the column header: it contains
    'county' alongside data keywords and has several non-empty cells (so a
    one-cell table-title row is not mistaken for the header)."""
    for i in range(min(15, len(df_raw))):
        vals = [str(c).strip() for c in df_raw.iloc[i].tolist()
                if str(c).strip() and str(c).strip().lower() != "nan"]
        if len(vals) < 3:
            continue
        cells = " ".join(v.lower() for v in vals)
        if "county" in cells and any(k in cells for k in (
                "population", "household", "area", "density", "male", "female", "total")):
            return i
    return 0


def _map_columns(header_cells):
    """Map source header cells to canonical names by keyword, first match wins.
    'female'/'male' are guarded so 'male' does not swallow 'female'."""
    mapping = {}
    used = set()
    lowered = [str(h).strip().lower() for h in header_cells]
    for canon, keys in _COLMAP:
        for idx, cell in enumerate(lowered):
            if idx in used:
                continue
            if canon == "population_male" and "female" in cell:
                continue
            if canon == "county" and ("sub" in cell):
                continue
            if any(k in cell for k in keys):
                mapping[idx] = canon
                used.add(idx)
                break
    return mapping


def _read_knbs_sheet(path: Path, level: str, xwalk):
    """Parse one KNBS XLSX into a tidy frame at county or sub-county level."""
    import pandas as pd

    raw = pd.read_excel(path, header=None, dtype=object)
    hrow = _detect_header(raw)
    header = raw.iloc[hrow].tolist()
    mapping = _map_columns(header)
    key = "subcounty" if level == "subcounty" else "county"
    if key not in mapping.values():
        return None
    body = raw.iloc[hrow + 1:].reset_index(drop=True)
    cols = {idx: name for idx, name in mapping.items()}
    out = body.iloc[:, list(cols.keys())].copy()
    out.columns = [cols[i] for i in cols]
    namecol = "subcounty" if level == "subcounty" else "county"
    if namecol not in out.columns:
        return None
    out[namecol] = out[namecol].astype(str).str.strip()
    out = out[out[namecol].str.len() > 0]
    # drop non-data rows (totals, blanks, footnotes)
    out = out[~out[namecol].str.lower().str.contains(
        r"total|kenya|source|note|^nan$|county$", regex=True, na=False)]
    for c in out.columns:
        if c not in ("county", "subcounty"):
            out[c] = pd.to_numeric(
                out[c].astype(str).str.replace(",", "").str.replace(r"[^0-9.\-]", "", regex=True),
                errors="coerce")
    out["county_norm" if level == "county" else "subcounty_norm"] = out[namecol].map(norm)
    # join to crosswalk
    if level == "county":
        cmap = xwalk.drop_duplicates("county_norm").set_index("county_norm")[["county_code", "county_name"]]
        out = out.join(cmap, on="county_norm")
    else:
        cmap = (xwalk.drop_duplicates("subcounty_norm")
                .set_index("subcounty_norm")[["subcounty_code", "subcounty_name", "county_code", "county_name"]])
        out = out.join(cmap, on="subcounty_norm")
    return out.reset_index(drop=True)


def census_population(base: Path):
    """Parse the KNBS county and sub-county XLSX into geography tables."""
    import pandas as pd

    base = Path(base)
    xwalk_path = base / "data" / "processed" / "crosswalk_admin.csv"
    if not xwalk_path.exists():
        print("[census] crosswalk not built yet - run the crosswalk step first")
        return []
    xwalk = pd.read_csv(xwalk_path)

    xlsx = _find(base, "kphc_2019_vol1", {".xlsx", ".xls"})
    if not xlsx:
        return []
    proc = base / "data" / "processed" / "geography"
    proc.mkdir(parents=True, exist_ok=True)
    written = []
    for level, hint in (("subcounty", "sub"), ("county", None)):
        # pick the most specific file for the level
        cands = [p for p in xlsx if (("sub" in p.name.lower()) == (level == "subcounty"))]
        for p in cands:
            try:
                df = _read_knbs_sheet(p, level, xwalk)
            except Exception as exc:  # noqa: BLE001
                print(f"[census] {p.name}: parse error {type(exc).__name__}: {exc}")
                continue
            if df is not None and not df.empty and df.get(
                    "county_code", pd.Series(dtype=object)).notna().any():
                out = proc / f"census_population_{level}.csv"
                df.to_csv(out, index=False)
                matched = int(df["county_code"].notna().sum())
                print(f"[census] {level}: {len(df)} rows ({matched} crosswalk-matched) "
                      f"from {p.name} -> {out.name}")
                written.append(out)
                break
    return written


# --------------------------------------------------------------------------
# 2. local PDF provenance + full text
# --------------------------------------------------------------------------
def _extract_text(pdf: Path):
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf)) as doc:
            pages = [(p.extract_text() or "") for p in doc.pages]
        return "\n".join(pages), len(pages)
    except Exception:  # noqa: BLE001
        try:
            from pypdf import PdfReader
            r = PdfReader(str(pdf))
            return "\n".join((pg.extract_text() or "") for pg in r.pages), len(r.pages)
        except Exception:  # noqa: BLE001
            return "", 0


def record_local_pdf(base: Path, source: str, title: str, note: str) -> dict | None:
    """Record provenance and extractable text of a locally-provided census PDF."""
    pdfs = _find(base, source, {".pdf"})
    if not pdfs:
        return None
    pdf = max(pdfs, key=lambda p: p.stat().st_size)
    proc = Path(base) / "data" / "processed" / "geography"
    proc.mkdir(parents=True, exist_ok=True)
    text, pages = _extract_text(pdf)
    chars = len(text.strip())
    if chars:
        (proc / f"{source}_fulltext.txt").write_text(text, encoding="utf-8")
    manifest = {
        "source_key": source,
        "title": title,
        "publisher": "Kenya National Bureau of Statistics",
        "layer": "geography",
        "access": "open_download (local)",
        "pdf_path": str(pdf),
        "sha256": _sha256(pdf),
        "bytes": pdf.stat().st_size,
        "pages": pages,
        "text_chars": chars,
        "message": (f"local document: {pages} pages, {chars} extractable chars; "
                    + note),
        "extracted_by": "Aboubacar HEMA",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    (proc / f"{source}_provenance.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[census] recorded {source}: {pages} pages, {chars} extractable chars "
          f"({'text' if chars > 5000 else 'graphics/scanned - little text'})")
    return manifest


def run(base: Path) -> dict:
    base = Path(base)
    result = {"xlsx_tables": [], "documents": []}
    result["xlsx_tables"] = [str(p) for p in census_population(base)]
    vol1 = record_local_pdf(
        base, "kphc_2019_vol1", "2019 Kenya Population and Housing Census Volume I",
        "structured county and sub-county denominators come from the KNBS XLSX tables")
    ag = record_local_pdf(
        base, "census_ke_ag", "2019 Kenya Population and Housing Census Analytical Report on Agriculture",
        "graphics-and-narrative report; county agricultural figures come from the KNBS data tables")
    result["documents"] = [m for m in (vol1, ag) if m]
    if not result["xlsx_tables"]:
        print("[census] no KNBS XLSX present yet - download them (see config/sources.yaml "
              "kphc_2019_vol1 urls) to build the county denominators")
    return result


if __name__ == "__main__":
    run(Path(__file__).resolve().parents[1])
