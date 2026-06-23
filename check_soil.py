#!/usr/bin/env python3
"""Layer 2 (Soil) diagnostic.

Checks every soil source folder under data/raw/, the processed zonal-statistics
table, and the database, then prints a per-source verdict so you can see at a
glance whether the soil data are downloaded and analysis-ready.

Run from the project root:  python check_soil.py
"""
from __future__ import annotations

from pathlib import Path

BASE = Path(__file__).resolve().parent
RAW = BASE / "data" / "raw"
PROC = BASE / "data" / "processed" / "soil"
DB = BASE / "data" / "db" / "kenya_fnp.duckdb"

# role of each soil source for the Kenya county analysis
ROLE = {
    "soilgrids": ("ESSENTIAL", "gridded soil-health backbone -> county zonal statistics"),
    "kensoter": ("SUPPLEMENTARY", "Kenya 1:1M legacy SOTER soil classes (vector)"),
    "afsis_chem": ("OPTIONAL", "Africa-wide spectra + reference chemistry (ML calibration)"),
    "kenya_soil_mirror": ("DEPRECATED", "superseded by kensoter; SODMA mirror offline"),
    "soilhive_ocp": ("MANUAL", "micronutrient points, released by request (agreement)"),
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
    print("Kenya FNP - Layer 2 (Soil) diagnostic")
    print("=" * 52)

    # ----- raw folders -------------------------------------------------------
    for src, (tier, desc) in ROLE.items():
        files, size = folder_stats(RAW / src)
        exts = sorted({p.suffix.lower() for p in files if p.suffix})
        print(f"\n[{src}]  {tier}")
        print(f"  {desc}")
        if not files:
            note = ("expected - released on request" if tier == "MANUAL"
                    else "expected - deprecated, safe to skip" if tier == "DEPRECATED"
                    else "NOT DOWNLOADED")
            print(f"  raw/: empty  ({note})")
            continue
        print(f"  raw/: {len(files)} files, {human(size)}, types={exts or 'n/a'}")
        if src == "soilgrids":
            tifs = [p for p in files if p.suffix.lower() == ".tif"]
            verdict = ("PASS" if len(tifs) >= 40 else "WARN" if tifs else "FAIL")
            print(f"  GeoTIFF coverages: {len(tifs)} "
                  f"(expect ~55 = 9 properties x 6 depths + ocs 0-30cm)  -> {verdict}")
        if src == "kensoter":
            zips = [p for p in files if p.suffix.lower() == ".zip"]
            shp = [p for p in files if p.suffix.lower() == ".shp"]
            print(f"  zip: {len(zips)}, extracted .shp: {len(shp)} "
                  f"-> {'PASS' if (zips or shp) else 'FAIL'}")

    # ----- processed zonal table --------------------------------------------
    print("\n" + "-" * 52)
    zon = PROC / "soilgrids_zonal_county.csv"
    if zon.exists():
        import csv
        with open(zon, encoding="utf-8") as fh:
            r = list(csv.reader(fh))
        ncols = len(r[0]) if r else 0
        print(f"processed/soil/soilgrids_zonal_county.csv: {len(r) - 1} county rows, "
              f"{ncols} columns -> {'PASS' if len(r) - 1 == 47 else 'WARN'}")
    else:
        print("processed/soil/soilgrids_zonal_county.csv: MISSING "
              "(run the transform step)")

    # ----- database ----------------------------------------------------------
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
    soil_tbls = con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='soil' ORDER BY table_name").fetchall()
    print(f"soil.* tables in database: {len(soil_tbls)}")
    for (t,) in soil_tbls:
        n = con.execute(f'SELECT count(*) FROM soil."{t}"').fetchone()[0]
        line = f"  soil.{t}: {n} rows"
        if t == "soilgrids_zonal_county":
            cols = [c[0] for c in con.execute(
                f'SELECT * FROM soil."{t}" LIMIT 0').description]
            meas = [c for c in cols if c not in ("county_code", "county_name", "county_norm")]
            nulls = con.execute(
                f'SELECT count(*) FROM soil."{t}" WHERE '
                + " OR ".join(f'"{c}" IS NULL' for c in meas)).fetchone()[0] if meas else 0
            line += f", {len(meas)} soil measures, rows-with-any-null={nulls}"
            line += "  -> PASS" if (n == 47 and nulls == 0) else "  -> WARN"
        print(line)

    # provenance for soil sources
    try:
        prov = con.execute(
            "SELECT source_key, status, count(*) FROM provenance "
            "WHERE layer='soil' GROUP BY source_key, status "
            "ORDER BY source_key, status").fetchall()
        if prov:
            print("\nprovenance (soil layer):")
            for sk, st, c in prov:
                print(f"  {sk:<20} {st:<8} {c}")
    except Exception:  # noqa: BLE001
        pass
    con.close()


if __name__ == "__main__":
    main()
