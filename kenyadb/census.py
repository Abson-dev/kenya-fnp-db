"""2019 Kenya Population and Housing Census: geography-layer denominators.

Two jobs, both run locally:

1. census_population(): build tidy county and sub-county denominators
   (population, households, land area, density), joined to the crosswalk, from
   whatever is present in data/raw/kphc_2019_vol1/ : the locally-provided
   Volume I PDF (parsed by table extraction, keyed on the crosswalk names so it
   is robust to layout), or the KNBS XLSX data tables if they have been
   downloaded. The XLSX are cleaner; the PDF path means it also runs offline
   from the file you already have.

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


def _num_cell(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    s = re.sub(r"[^0-9.\-]", "", s)
    if s in ("", "-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _classify_tables(tables, xwalk):
    """Turn raw extracted tables (lists of rows) into county and sub-county
    records, keyed on the crosswalk names so the result is robust to the exact
    page layout. A row is a county row if its name matches a county, a
    sub-county row if it matches a sub-county."""
    import pandas as pd

    county_norms = set(xwalk["county_norm"]) - {""}
    sub_norms = set(xwalk["subcounty_norm"].dropna()) - {""}
    county_rows, sub_rows = [], []
    val_canons = {"population_total", "population_male", "population_female",
                  "households", "avg_household_size", "land_area_sqkm", "density"}

    for tbl in tables:
        if not tbl or len(tbl) < 2:
            continue
        header_idx, mapping = None, None
        for hi in range(min(12, len(tbl))):
            row = tbl[hi]
            nonempty = sum(1 for c in row if c is not None and str(c).strip() != "")
            m = _map_columns(row)
            if (val_canons & set(m.values())) and nonempty >= 3:
                header_idx, mapping = hi, m
                break
        if mapping is None:
            continue
        inv = {v: k for k, v in mapping.items()}
        name_idx = inv.get("subcounty", inv.get("county"))
        if name_idx is None:
            name_idx = 0                      # header had no name label: assume col 0
        for row in tbl[header_idx + 1:]:
            if not row or name_idx >= len(row) or row[name_idx] is None:
                continue
            nm = str(row[name_idx]).replace("\n", " ").strip()
            if not nm:
                continue
            nn = norm(nm)
            rec = {}
            for idx, canon in mapping.items():
                if canon in ("county", "subcounty") or idx >= len(row):
                    continue
                rec[canon] = _num_cell(row[idx])
            # Prefer county: a handful of county names are also sub-county names,
            # and the population data tables are county-level, so county wins ties.
            if nn in county_norms:
                rec["county_norm"] = nn
                county_rows.append(rec)
            elif nn in sub_norms:
                rec["subcounty_norm"] = nn
                sub_rows.append(rec)

    def _finish(rows, key, joincols):
        if not rows:
            return None
        df = pd.DataFrame(rows).dropna(how="all", subset=[c for c in rows[0] if c != key])
        df = df.drop_duplicates(key, keep="first")
        cmap = xwalk.drop_duplicates(key).set_index(key)[joincols]
        return df.join(cmap, on=key).reset_index(drop=True)

    county_df = _finish(county_rows, "county_norm", ["county_code", "county_name"])
    sub_df = _finish(sub_rows, "subcounty_norm",
                     ["subcounty_code", "subcounty_name", "county_code", "county_name"])
    return county_df, sub_df


def _read_knbs_pdf(pdf: Path, xwalk):
    """Extract county and sub-county denominator tables from the Volume I PDF.
    Tries ruled-table extraction first; if that finds nothing (the KNBS tables
    often have no cell borders), falls back to parsing the text rows."""
    import pdfplumber

    tables, lines = [], []
    with pdfplumber.open(str(pdf)) as doc:
        for pg in doc.pages:
            tables.extend(pg.extract_tables() or [])
            lines.extend((pg.extract_text() or "").split("\n"))
    county_df, sub_df = _classify_tables(tables, xwalk)
    if (county_df is None or county_df.empty) and (sub_df is None or sub_df.empty):
        county_df, sub_df = _parse_knbs_text(lines, xwalk)
    return county_df, sub_df


# KNBS population tables, by number of numeric columns after the name. Order
# follows the published "by Sex, Number of Households, Land Area, Population
# Density" layout; used only when the header keywords cannot be read directly.
_KNBS_COLSETS = {
    8: ["population_male", "population_female", "intersex", "population_total",
        "households", "avg_household_size", "land_area_sqkm", "density"],
    7: ["population_male", "population_female", "intersex", "population_total",
        "households", "land_area_sqkm", "density"],
    6: ["population_male", "population_female", "population_total",
        "households", "land_area_sqkm", "density"],
    5: ["population_male", "population_female", "population_total", "households", "density"],
    4: ["population_total", "households", "land_area_sqkm", "density"],
    3: ["population_total", "households", "density"],
}
_HEADER_KEYS = [
    ("population_male", ["male"]), ("population_female", ["female"]),
    ("intersex", ["intersex"]), ("population_total", ["total"]),
    ("households", ["household"]), ("avg_household_size", ["average", "mean hh", "av. hh"]),
    ("land_area_sqkm", ["land area", "sq. km", "sq km", "area"]), ("density", ["density"]),
]
_NUMTOK = re.compile(r"^-?[\d,]+(?:\.\d+)?$")


def _header_order(text: str):
    """Return the canonical column names in their order of appearance in a
    header string, located by keyword position (robust to multi-word labels)."""
    low = text.lower()
    pos = []
    for canon, kws in _HEADER_KEYS:
        for kw in kws:
            i = low.find(kw)
            if i >= 0:
                pos.append((i, canon))
                break
    pos.sort()
    order = []
    for _, c in pos:
        if c not in order:
            order.append(c)
    return order


def _parse_knbs_text(lines, xwalk):
    """Parse county and sub-county rows from the extracted text of the Volume I
    PDF. A data row is a county or sub-county name (1-3 tokens, matched to the
    crosswalk) followed by numeric tokens. Columns are labelled from the header
    where readable, otherwise by the standard KNBS column set for that count."""
    import pandas as pd

    county_norms = set(xwalk["county_norm"]) - {""}
    sub_norms = set(xwalk["subcounty_norm"].dropna()) - {""} if "subcounty_norm" in xwalk else set()

    # header column order, taken from the line with the most keyword hits
    order = []
    for ln in lines:
        o = _header_order(ln)
        if len(o) > len(order):
            order = o

    def parse_row(toks):
        for k in (3, 2, 1):
            if len(toks) <= k:
                continue
            nn = norm(" ".join(toks[:k]))
            if nn in county_norms or nn in sub_norms:
                vals = []
                for t in toks[k:]:
                    if _NUMTOK.match(t):
                        vals.append(float(t.replace(",", "")))
                if len(vals) >= 3:
                    return nn, vals
        return None, None

    county_rows, sub_rows = [], []
    for ln in lines:
        toks = ln.split()
        if not toks:
            continue
        nn, vals = parse_row(toks)
        if nn is None:
            continue
        (sub_rows if nn in sub_norms else county_rows).append((nn, vals))

    def build(rows, key, joincols):
        if not rows:
            return None
        ncol = max((len(v) for _, v in rows), key=[len(v) for _, v in rows].count)
        cols = order if len(order) == ncol else _KNBS_COLSETS.get(
            ncol, [f"val_{i+1}" for i in range(ncol)])
        recs = []
        for nn, vals in rows:
            if len(vals) != ncol:
                continue
            rec = {key: nn}
            rec.update({c: v for c, v in zip(cols, vals)})
            recs.append(rec)
        if not recs:
            return None
        df = pd.DataFrame(recs).drop_duplicates(key, keep="first")
        cmap = xwalk.drop_duplicates(key).set_index(key)[joincols]
        return df.join(cmap, on=key).reset_index(drop=True)

    county_df = build(county_rows, "county_norm", ["county_code", "county_name"])
    sub_df = build(sub_rows, "subcounty_norm",
                   ["subcounty_code", "subcounty_name", "county_code", "county_name"])
    return county_df, sub_df


def _load_table(path: Path):
    """Read a CSV or Excel data table, skipping any leading title rows so the
    real header (the row containing 'county') becomes the columns."""
    import pandas as pd

    if path.suffix.lower() in (".xlsx", ".xls"):
        raw = pd.read_excel(path, header=None, dtype=object)
    else:
        raw = pd.read_csv(path, header=None, dtype=object, on_bad_lines="skip")
    hrow = 0
    for i in range(min(15, len(raw))):
        cells = " ".join(str(c).lower() for c in raw.iloc[i].tolist())
        if "county" in cells:
            hrow = i
            break
    df = raw.iloc[hrow + 1:].copy()
    # build unique, non-blank column names (the KNBS CSV has blank/duplicate headers)
    seen, names = {}, []
    for j, c in enumerate(raw.iloc[hrow].tolist()):
        nm = str(c).strip()
        if nm == "" or nm.lower() == "nan":
            nm = f"col_{j}"
        if nm in seen:
            seen[nm] += 1
            nm = f"{nm}_{seen[nm]}"
        else:
            seen[nm] = 0
        names.append(nm)
    df.columns = names
    return df.reset_index(drop=True)


def _file_to_rows(path: Path):
    """Read a CSV or Excel file into a list of rows (list of cells)."""
    if path.suffix.lower() in (".xlsx", ".xls"):
        import pandas as pd
        return pd.read_excel(path, header=None, dtype=object).values.tolist()
    import csv
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        return list(csv.reader(fh))


def _ag_colname(group: str, sub: str, idx: int) -> str:
    """Clean column name for the agriculture table from its two header cells."""
    g, s = (group or "").lower(), (sub or "").lower()
    measure = "ag_land_ha" if "area" in g or "agricultural land" in g else (
        "farming_households" if "household" in g else f"col_{idx}")
    if "subsistence" in s:
        return f"{measure}_subsistence"
    if "commercial" in s:
        return f"{measure}_commercial"
    return f"{measure}_total"


def _ag_num(v):
    if v is None:
        return None
    s = re.sub(r"[^0-9.\-]", "", str(v).replace(",", ""))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


def census_agriculture(base: Path):
    """Parse the KNBS agricultural-land and farming-households table (DISTRI /
    openAFRICA Census Volume IV) into county and sub-county tables.

    The table has a two-row header (a measure group on the first row, the
    subsistence / commercial split on the second) and lists the national total,
    then each county total, then that county's sub-counties. Counties and
    sub-counties are told apart by the crosswalk, and county rows double as the
    section header that assigns each sub-county to its parent.
    """
    import pandas as pd

    base = Path(base)
    xwalk_path = base / "data" / "processed" / "crosswalk_admin.csv"
    if not xwalk_path.exists():
        return []
    xwalk = pd.read_csv(xwalk_path)
    if "subcounty_norm" not in xwalk.columns:
        xwalk["subcounty_norm"] = None
    county_norms = set(xwalk["county_norm"]) - {""}
    sub_norms = set(xwalk["subcounty_norm"].dropna()) - {""}

    files = [p for p in _find(base, "census_ke_ag", {".csv", ".xlsx", ".xls"})]
    if not files:
        return []
    # if several tables sit in the folder, prefer the agriculture distribution file
    files.sort(key=lambda p: 0 if any(k in p.name.lower() for k in
               ("distribution", "agric", "distri", "farming", "land")) else 1)
    proc = base / "data" / "processed" / "geography"
    proc.mkdir(parents=True, exist_ok=True)

    rows = _file_to_rows(files[0])
    if len(rows) < 3:
        return []
    # locate the name header row, then merge it with the next row for column names
    hrow = 0
    for i in range(min(6, len(rows))):
        cells = " ".join(str(c).lower() for c in rows[i])
        if "county" in cells:
            hrow = i
            break
    g_row = [str(c or "").strip() for c in rows[hrow]]
    s_row = [str(c or "").strip() for c in rows[hrow + 1]] if hrow + 1 < len(rows) else []
    # carry forward merged group cells (blank cells inherit the last group)
    for j in range(1, len(g_row)):
        if not g_row[j]:
            g_row[j] = g_row[j - 1]
    ncol = len(g_row)
    colnames = ["name"] + [_ag_colname(g_row[j], s_row[j] if j < len(s_row) else "", j)
                           for j in range(1, ncol)]
    # the second header row is data only if it holds no subsistence/commercial labels
    data_start = hrow + 2 if any("subsist" in c.lower() or "commerc" in c.lower()
                                 for c in s_row) else hrow + 1

    county_recs, sub_recs, current = [], [], None
    for r in rows[data_start:]:
        if not r or not str(r[0]).strip():
            continue
        name = str(r[0]).strip()
        nn = norm(name)
        if nn in ("kenya", ""):
            continue
        vals = {colnames[j]: _ag_num(r[j]) for j in range(1, min(ncol, len(r)))}
        if nn in county_norms and (current is None or nn != current):
            current = nn
            county_recs.append({"county_norm": nn, **vals})
        elif nn in sub_norms:
            sub_recs.append({"subcounty_norm": nn, **vals})
        elif nn in county_norms:
            county_recs.append({"county_norm": nn, **vals})

    written = []
    if county_recs:
        c = pd.DataFrame(county_recs).drop_duplicates("county_norm", keep="first")
        cmap = xwalk.drop_duplicates("county_norm").set_index("county_norm")[["county_code", "county_name"]]
        c = c.join(cmap, on="county_norm")
        c = c[c["county_code"].notna()]
        out = proc / "census_agriculture_county.csv"
        c.to_csv(out, index=False)
        print(f"[census] agriculture county: {len(c)} rows -> {out.name}")
        written.append(out)
    if sub_recs:
        s = pd.DataFrame(sub_recs).drop_duplicates("subcounty_norm", keep="first")
        smap = (xwalk.dropna(subset=["subcounty_norm"]).drop_duplicates("subcounty_norm")
                .set_index("subcounty_norm")[["subcounty_code", "subcounty_name", "county_code", "county_name"]])
        s = s.join(smap, on="subcounty_norm")
        s = s[s["subcounty_code"].notna()]
        out = proc / "census_agriculture_subcounty.csv"
        s.to_csv(out, index=False)
        print(f"[census] agriculture sub-county: {len(s)} rows matched -> {out.name}")
        written.append(out)
    return written


def _merge_on(frames, key):
    """Outer-merge frames on a key, coalescing duplicate measure columns."""
    import pandas as pd
    out = None
    for df in frames:
        if df is None or df.empty:
            continue
        if out is None:
            out = df.copy()
            continue
        dup = [c for c in df.columns if c in out.columns and c != key]
        out = out.merge(df, on=key, how="outer", suffixes=("", "_b"))
        for c in dup:
            if f"{c}_b" in out.columns:
                out[c] = out[c].fillna(out[f"{c}_b"])
                out = out.drop(columns=[f"{c}_b"])
    return out


def census_population(base: Path):
    """Build the county (and sub-county where available) population denominators
    from data/raw/kphc_2019_vol1/ : the KNBS data-table CSV/XLSX exports if
    present (merged across files), else the local Volume I PDF. Writes one CSV
    per level that yields data."""
    import pandas as pd

    base = Path(base)
    xwalk_path = base / "data" / "processed" / "crosswalk_admin.csv"
    if not xwalk_path.exists():
        print("[census] crosswalk not built yet - run the crosswalk step first")
        return []
    xwalk = pd.read_csv(xwalk_path)
    if "subcounty_norm" not in xwalk.columns:
        xwalk["subcounty_norm"] = None
    proc = base / "data" / "processed" / "geography"
    proc.mkdir(parents=True, exist_ok=True)

    county_frames, sub_frames = [], []

    # 1. KNBS data-table exports (CSV or XLSX). Each is fed to the same name-keyed
    #    classifier; the two county files (population/land/density and
    #    population/households) merge into one county table.
    data_files = _find(base, "kphc_2019_vol1", {".csv", ".xlsx", ".xls"})
    for p in data_files:
        try:
            rows = _file_to_rows(p)
            c_df, s_df = _classify_tables([rows], xwalk)
        except Exception as exc:  # noqa: BLE001
            print(f"[census] {p.name}: parse error {type(exc).__name__}: {exc}")
            continue
        if c_df is not None and not c_df.empty:
            county_frames.append(c_df)
        if s_df is not None and not s_df.empty:
            sub_frames.append(s_df)

    # 2. fall back to the local Volume I PDF for any level still missing
    if not county_frames or not sub_frames:
        pdfs = _find(base, "kphc_2019_vol1", {".pdf"})
        if pdfs:
            pdf = max(pdfs, key=lambda p: p.stat().st_size)
            try:
                c_df, s_df = _read_knbs_pdf(pdf, xwalk)
            except Exception as exc:  # noqa: BLE001
                print(f"[census] {pdf.name}: PDF parse error {type(exc).__name__}: {exc}")
                c_df = s_df = None
            if not county_frames and c_df is not None and not c_df.empty:
                county_frames.append(c_df)
            if not sub_frames and s_df is not None and not s_df.empty:
                sub_frames.append(s_df)

    written = []
    for level, frames, key in (("county", county_frames, "county_code"),
                               ("subcounty", sub_frames, "subcounty_code")):
        merged = _merge_on(frames, key)
        if merged is None or merged.empty:
            continue
        out = proc / f"census_population_{level}.csv"
        merged.to_csv(out, index=False)
        matched = int(merged["county_code"].notna().sum())
        print(f"[census] population {level}: {len(merged)} rows "
              f"({matched} crosswalk-matched) -> {out.name}")
        written.append(out)

    if not written:
        print("[census] kphc_2019_vol1: no population data produced. Provide the KNBS "
              "data-table CSV/XLSX in data/raw/kphc_2019_vol1/, or a machine-readable "
              "Volume I PDF.")
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


def _safe(label, fn, *args):
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"[census] {label}: error {type(exc).__name__}: {exc} (continuing)")
        traceback.print_exc()
        return []


def run(base: Path) -> dict:
    base = Path(base)
    result = {"tables": [], "documents": []}
    # county and sub-county denominators from the Volume I PDF or the KNBS XLSX
    result["tables"] += [str(p) for p in _safe("census_population", census_population, base)]
    # agricultural land and farming households from the KNBS data table (CSV)
    result["tables"] += [str(p) for p in _safe("census_agriculture", census_agriculture, base)]
    # provenance and full text for the locally-provided census PDFs
    vol1 = _safe("record kphc_2019_vol1", record_local_pdf, base, "kphc_2019_vol1",
                 "2019 Kenya Population and Housing Census Volume I",
                 "county and sub-county denominators are extracted from this PDF (or the KNBS XLSX if downloaded)")
    ag = _safe("record census_ke_ag", record_local_pdf, base, "census_ke_ag",
               "2019 Kenya Population and Housing Census Analytical Report on Agriculture",
               "graphics-and-narrative report; the structured county agricultural figures come from the "
               "KNBS census data table, not from this report")
    result["documents"] = [m for m in (vol1, ag) if m and not isinstance(m, list)]
    return result


if __name__ == "__main__":
    run(Path(__file__).resolve().parents[1])
