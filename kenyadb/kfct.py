"""Kenya Food Composition Tables 2018 (KFCT): food-layer reference extractor.

The KFCT is a fixed published PDF whose nutrient tables are machine-readable.
This module reads the PDF placed in data/raw/kfct_2018/ and writes tidy,
analysis-ready CSVs to data/processed/food/ , one per nutrient block plus a
merged food table keyed on the 5-digit food code:

  kfct_proximates.csv   edible factor, energy, water, protein, fat, carbohydrate, fibre, ash
  kfct_minerals.csv     Ca Fe Mg P K Na Zn Se
  kfct_vitamins.csv     vitamin A, retinol, carotene, thiamin, riboflavin, niacin, folate, B12, C
  kfct_foods.csv        the three blocks merged on food_code
  kfct_provenance.json  checksum, page count, row counts (folded into the ledger)

Tables are read with pdfplumber.extract_tables(), which preserves blank cells by
column position (more robust than token parsing when a nutrient value is absent).
Food-group headers (a 2-digit code) and the "n" / "SD or min-max" rows are
skipped. Values such as "tr" (trace) map to 0 and blanks map to missing.

Author: Aboubacar HEMA
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

CODE5 = re.compile(r"^\d{5}$")
CODE2 = re.compile(r"^\d{2}$")
SKIP_FIRST = ("n", "sd or min-max", "sd", "mean", "median", "min", "max",
              "min-max", "range", "%", "")

# Each block: detection tokens that must all appear in the header row, and the
# output column names in column order after (Code, Food name).
BLOCKS = {
    "proximates": {
        "detect": ("energy", "protein", "water"),
        "cols": ["edible_factor", "energy_kj", "energy_kcal", "water_g",
                 "protein_g", "fat_g", "carbohydrate_g", "fibre_g", "ash_g"],
    },
    "minerals": {
        "detect": ("ca", "fe", "zn", "se"),
        "cols": ["ca_mg", "fe_mg", "mg_mg", "p_mg", "k_mg", "na_mg", "zn_mg", "se_mcg"],
    },
    "vitamins": {
        "detect": ("retinol", "thiamin", "riboflavin"),
        "cols": ["vit_a_rae_mcg", "vit_a_re_mcg", "retinol_mcg", "beta_carotene_mcg",
                 "thiamin_mg", "riboflavin_mg", "niacin_mg", "folate_dfe_mcg",
                 "food_folate_mcg", "vit_b12_mcg", "vit_c_mg"],
    },
}


def find_pdf(base: Path) -> Path | None:
    d = Path(base) / "data" / "raw" / "kfct_2018"
    if not d.exists():
        return None
    pdfs = sorted(d.glob("*.pdf")) + sorted(d.glob("*.PDF"))
    return pdfs[0] if pdfs else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _num(cell: str):
    """Parse a KFCT cell into a float or None. Blank and '-' are missing;
    'tr' (trace) is 0; ranges and footnote markers are treated as missing."""
    if cell is None:
        return None
    s = str(cell).strip().replace(",", "")
    if s in ("", "-", "...", "[]", "na", "NA", "n.d.", "nd"):
        return None
    if s.lower() in ("tr", "trace", "[0]", "0"):
        return 0.0
    m = re.match(r"^-?\d+(?:\.\d+)?$", s)
    if m:
        return float(s)
    m = re.match(r"^\[(-?\d+(?:\.\d+)?)\]$", s)  # bracketed estimate
    if m:
        return float(m.group(1))
    return None


def _detect_block(header: list) -> str | None:
    cells = " ".join((c or "").lower() for c in header)
    for name, spec in BLOCKS.items():
        if all(tok in cells for tok in spec["detect"]):
            return name
    return None


def extract(pdf: Path):
    """Parse the PDF into {block_name: list of row dicts}. Each row carries
    food_code, food_group, food_name and the block's nutrient columns.

    The nutrient block is detected from the page text header (which repeats on
    every page), then every row of every table on that page is classified by
    its code, so continuation tables that do not repeat the header are still
    captured."""
    import pdfplumber

    out = {b: [] for b in BLOCKS}
    n_pages = 0
    with pdfplumber.open(str(pdf)) as doc:
        n_pages = len(doc.pages)
        group = {b: None for b in BLOCKS}
        for pg in doc.pages:
            head = " ".join((pg.extract_text() or "").split("\n")[:6])
            block = _detect_block([head])
            if block is None:
                continue
            cols = BLOCKS[block]["cols"]
            for tbl in pg.extract_tables():
                if not tbl:
                    continue
                for row in tbl:
                    if not row or row[0] is None:
                        continue
                    code = str(row[0]).strip()
                    if CODE2.match(code):                       # food-group header
                        name = (row[1] or "").replace("\n", " ").strip() if len(row) > 1 else ""
                        group[block] = f"{code} {name}".strip()
                        continue
                    if not CODE5.match(code):                   # header / n / SD / blank
                        continue
                    name = (row[1] or "").replace("\n", " ").strip() if len(row) > 1 else ""
                    rec = {"food_code": code, "food_group": group[block],
                           "food_name": name}
                    vals = list(row[2:2 + len(cols)])
                    vals += [None] * (len(cols) - len(vals))    # pad short rows
                    for c, v in zip(cols, vals):
                        rec[c] = _num(v)
                    out[block].append(rec)
    return out, n_pages


def run(base: Path) -> dict:
    """Extract the KFCT PDF and write the food-composition CSVs locally."""
    import pandas as pd

    base = Path(base)
    proc = base / "data" / "processed" / "food"
    proc.mkdir(parents=True, exist_ok=True)

    pdf = find_pdf(base)
    if pdf is None:
        print("[kfct] no PDF at data/raw/kfct_2018/*.pdf - skipping")
        return {}

    blocks, n_pages = extract(pdf)
    frames = {}
    for name, rows in blocks.items():
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.drop_duplicates("food_code", keep="first")
        frames[name] = df
        if not df.empty:
            df.to_csv(proc / f"kfct_{name}.csv", index=False)

    # merged food table on food_code, coalescing name/group across blocks
    name_map, group_map = {}, {}
    for name in ("proximates", "minerals", "vitamins"):   # priority order
        df = frames.get(name)
        if df is None or df.empty:
            continue
        for _, r in df[["food_code", "food_name", "food_group"]].iterrows():
            name_map.setdefault(r["food_code"], r["food_name"])
            group_map.setdefault(r["food_code"], r["food_group"])

    merged = None
    for name in ("proximates", "minerals", "vitamins"):
        df = frames.get(name)
        if df is None or df.empty:
            continue
        keep = df.drop(columns=["food_group", "food_name"])
        merged = keep if merged is None else merged.merge(keep, on="food_code", how="outer")
    n_foods = 0
    if merged is not None and not merged.empty:
        merged.insert(1, "food_group", merged["food_code"].map(group_map))
        merged.insert(2, "food_name", merged["food_code"].map(name_map))
        merged = merged.sort_values("food_code").reset_index(drop=True)
        merged.to_csv(proc / "kfct_foods.csv", index=False)
        n_foods = len(merged)

    counts = {k: int(len(v)) for k, v in frames.items()}
    manifest = {
        "source_key": "kfct_2018",
        "title": "Kenya Food Composition Tables 2018",
        "publisher": "FAO and Government of Kenya",
        "layer": "food",
        "access": "open_download (local)",
        "pdf_path": str(pdf),
        "sha256": _sha256(pdf),
        "bytes": pdf.stat().st_size,
        "pages": n_pages,
        "foods_total": n_foods,
        "block_rows": counts,
        "message": (f"local extract: {n_pages} pages, {n_foods} foods, blocks "
                    + ", ".join(f"{k}={v}" for k, v in counts.items())),
        "extracted_by": "Aboubacar HEMA",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }
    (proc / "kfct_provenance.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[kfct] {n_pages} pages -> {n_foods} foods "
          f"(proximates={counts.get('proximates',0)}, minerals={counts.get('minerals',0)}, "
          f"vitamins={counts.get('vitamins',0)}) -> {proc}")
    return manifest


if __name__ == "__main__":
    run(Path(__file__).resolve().parents[1])
