#!/usr/bin/env python3
"""Layer 3 (Food) diagnostic.

Checks every food source (raw folders, processed CSVs, database tables) and
prints a per-source verdict so you can see whether the food data are downloaded
and analysis-ready.

Run from the project root:  python check_food.py
"""
from __future__ import annotations

from pathlib import Path

BASE = Path(__file__).resolve().parent
RAW = BASE / "data" / "raw"
EXT = BASE / "data" / "external"
PROC = BASE / "data" / "processed" / "food"
DB = BASE / "data" / "db" / "kenya_fnp.duckdb"

# source -> (tier, where it lands, description)
FOOD = {
    "kfct_2018": ("ESSENTIAL", RAW / "kfct_2018",
                  "Kenya Food Composition Tables (nutrient backbone)"),
    "faostat": ("ESSENTIAL", RAW / "faostat",
                "FAOSTAT national long-run time series"),
    "wfp_prices": ("ESSENTIAL", RAW / "wfp_prices",
                   "WFP observed monthly market prices"),
    "kilimostat": ("ESSENTIAL", RAW / "kilimostat",
                   "KNBS NAPR 2024: crop area + production by county/year"),
    "wb_rtfp": ("OPTIONAL", EXT / "wb_rtfp",
                "World Bank modelled gap-filled prices (manual download)"),
    "fsd_food": ("CONTEXT", RAW / "fsd_food",
                 "NIPFN Power BI dashboard (no data export; reference only)"),
}
# expected processed CSVs / DB tables per source
EXPECT_TABLES = {
    "kfct_2018": ["kfct_foods", "kfct_proximates", "kfct_minerals", "kfct_vitamins"],
    "faostat": ["faostat_kenya"],
    "wfp_prices": ["prices_wfp_observed"],
    "kilimostat": ["napr_crop_county"],
    "wb_rtfp": ["prices_wb_modeled"],
    "fsd_food": [],
}


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def folder_stats(d: Path):
    if not d.exists():
        return [], 0
    files = [p for p in d.rglob("*") if p.is_file()]
    return files, sum(p.stat().st_size for p in files)


def main() -> None:
    print("Kenya FNP - Layer 3 (Food) diagnostic")
    print("=" * 52)

    for src, (tier, folder, desc) in FOOD.items():
        files, size = folder_stats(folder)
        exts = sorted({p.suffix.lower() for p in files if p.suffix})
        print(f"\n[{src}]  {tier}")
        print(f"  {desc}")
        if not files:
            if tier == "CONTEXT":
                note = "expected - dashboard, no downloadable data"
            elif tier == "OPTIONAL":
                note = "not downloaded (manual / registration source)"
            else:
                note = "NOT DOWNLOADED"
            print(f"  {folder.relative_to(BASE)}/: empty  ({note})")
            continue
        print(f"  {folder.relative_to(BASE)}/: {len(files)} files, "
              f"{human(size)}, types={exts or 'n/a'}")
        if src == "kilimostat":
            pdfs = [p for p in files if p.suffix.lower() == ".pdf"]
            csvs = [p for p in files if p.suffix.lower() in (".csv", ".xlsx")]
            print(f"  NAPR report PDF: {len(pdfs)}, tabular exports: {len(csvs)} "
                  f"-> {'PASS' if (pdfs or csvs) else 'FAIL'}")

    # processed CSVs
    print("\n" + "-" * 52)
    if PROC.exists():
        csvs = sorted(p.name for p in PROC.glob("*.csv"))
        print(f"processed/food/: {len(csvs)} CSVs")
        for c in csvs:
            print(f"  {c}")
    else:
        print("processed/food/: none yet")

    # database
    print("-" * 52)
    if not DB.exists():
        print(f"database not built yet ({DB})")
        return
    try:
        import duckdb
    except ImportError:
        print("duckdb not installed - skipping database checks")
        return
    con = duckdb.connect(str(DB), read_only=True)
    have = {t for (t,) in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='food'").fetchall()}
    print(f"food.* tables in database: {len(have)}")
    for src, tables in EXPECT_TABLES.items():
        for t in tables:
            if t in have:
                n = con.execute(f'SELECT count(*) FROM food."{t}"').fetchone()[0]
                print(f"  food.{t}: {n} rows  -> PASS  [{src}]")
            elif FOOD[src][0] in ("ESSENTIAL",):
                print(f"  food.{t}: MISSING  -> pending  [{src}]")
    extra = have - {t for ts in EXPECT_TABLES.values() for t in ts}
    for t in sorted(extra):
        n = con.execute(f'SELECT count(*) FROM food."{t}"').fetchone()[0]
        print(f"  food.{t}: {n} rows  (additional)")

    try:
        prov = con.execute(
            "SELECT source_key, status, count(*) FROM provenance "
            "WHERE layer='food' GROUP BY source_key, status "
            "ORDER BY source_key, status").fetchall()
        if prov:
            print("\nprovenance (food layer):")
            for sk, st, c in prov:
                print(f"  {sk:<16} {st:<8} {c}")
    except Exception:  # noqa: BLE001
        pass
    con.close()


if __name__ == "__main__":
    main()
