"""Extract county agricultural expenditure from an Office of the Controller of
Budget, County Budget Implementation Review Report (CBIRR) PDF into the tidy CSV
the policy panel ingests.

    pip install pdfplumber
    python tools/parse_cob_expenditure.py path/to/CBIRR_FY2023-24.pdf --fy 2023/24

Writes data/external/cob_expenditure/cob_<fy>.csv with columns:
    county, fiscal_year, ag_budget_alloc_kes_m, ag_expenditure_kes_m
then run:
    python run_all.py --layer policy
    python analyze.py

Honest caveat. The CBIRRs are long PDFs and the per-county department tables vary
in layout and wording across years ("Agriculture", "Agriculture, Rural and Urban
Development", "Agriculture, Livestock and Fisheries"). This script is a best-effort
scaffold: it scans each page for a department table, reads the row whose label
starts with "Agric", and attributes it to the most recent county heading seen. It
prints what it found per county so you can spot-check, and it will not be perfect
on every report. Treat the output as a draft to verify, not a finished panel. The
cleanest path remains a direct request to the Controller of Budget or IFPRI for
the underlying county expenditure tables in spreadsheet form.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

COUNTY_HINT = re.compile(r"\b([A-Z][a-zA-Z' ]+?)\s+County\b")
AGRIC = re.compile(r"^\s*agric", re.IGNORECASE)
NUM = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def _to_m(x: str) -> float | None:
    try:
        v = float(x.replace(",", ""))
    except (ValueError, AttributeError):
        return None
    # CBIRR department tables are usually in KES millions already; if a value is
    # implausibly large it is probably in KES, so scale down.
    return round(v / 1_000_000, 2) if v > 1_000_000 else round(v, 2)


def parse(pdf_path: Path, fiscal_year: str) -> list[dict]:
    import pdfplumber

    rows: list[dict] = []
    seen: set[str] = set()
    with pdfplumber.open(str(pdf_path)) as pdf:
        current_county = None
        for page in pdf.pages:
            text = page.extract_text() or ""
            m = COUNTY_HINT.search(text)
            if m:
                current_county = m.group(1).strip()
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [c or "" for c in row]
                    label = cells[0] if cells else ""
                    if not AGRIC.match(label):
                        continue
                    nums = [n for c in cells[1:] for n in NUM.findall(c)]
                    if len(nums) < 2 or current_county is None:
                        continue
                    alloc, spent = _to_m(nums[0]), _to_m(nums[1])
                    key = current_county.lower()
                    if alloc is None or spent is None or key in seen:
                        continue
                    seen.add(key)
                    rows.append({"county": current_county, "fiscal_year": fiscal_year,
                                 "ag_budget_alloc_kes_m": alloc, "ag_expenditure_kes_m": spent})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("pdf", help="path to a CBIRR PDF")
    ap.add_argument("--fy", required=True, help="fiscal year label, e.g. 2023/24")
    ap.add_argument("--base", default=".", help="path to the kenya_fnp_db root")
    args = ap.parse_args()

    rows = parse(Path(args.pdf), args.fy)
    if not rows:
        print("No agriculture rows found. The table layout likely differs; inspect the PDF "
              "and adjust the AGRIC / COUNTY_HINT patterns.")
        return
    out_dir = Path(args.base) / "data" / "external" / "cob_expenditure"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"cob_{args.fy.replace('/', '-')}.csv"
    import csv
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["county", "fiscal_year",
                                          "ag_budget_alloc_kes_m", "ag_expenditure_kes_m"])
        w.writeheader()
        w.writerows(rows)
    print(f"Found agriculture rows for {len(rows)} counties -> {out}")
    for r in rows:
        print(f"  {r['county']:<18} alloc {r['ag_budget_alloc_kes_m']:>10}  "
              f"spent {r['ag_expenditure_kes_m']:>10}")
    print("\nSpot-check these against the report, then: python run_all.py --layer policy")


if __name__ == "__main__":
    main()
