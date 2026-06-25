#!/usr/bin/env python3
"""Layer 2 (Soil) check.

Verifies the SoilGrids backbone, the iSDAsoil micronutrients, the AfSIS
validation set, and that the soil index has every input it needs. Read-only;
needs no rebuild.

Run from the project root:  python check_soil.py
"""
from __future__ import annotations

from pathlib import Path

from kenyadb.checks import (CheckResult, columns, connect, coverage_check,
                            folder_stats, human, provenance_summary, table_check)

BASE = Path(__file__).resolve().parent


def run(base: Path = BASE) -> CheckResult:
    res = CheckResult("Soil")
    con = connect(base)

    # raw SoilGrids coverages (the gridded backbone)
    files, size = folder_stats(base / "data" / "raw" / "soilgrids")
    tifs = [p for p in files if p.suffix.lower() == ".tif"]
    if not files:
        res.warn("raw/soilgrids", "empty (not downloaded)")
    elif len(tifs) >= 40:
        res.ok("raw/soilgrids", f"{len(tifs)} GeoTIFFs, {human(size)} (expect ~55)")
    else:
        res.warn("raw/soilgrids", f"{len(tifs)} GeoTIFFs (expect ~55)")

    # processed zonal tables
    for name in ("soilgrids_zonal_county", "isda_county"):
        p = base / "data" / "processed" / "soil" / f"{name}.csv"
        res.ok(f"processed/soil/{name}.csv", "present") if p.exists() \
            else res.warn(f"processed/soil/{name}.csv", "missing")

    # database tables
    sg = table_check(res, con, "soil", "soilgrids_zonal_county", expect=47)
    isda = table_check(res, con, "soil", "isda_county", expect=47)
    # AfSIS is a sparse sentinel validation set; a low count is expected
    af = table_check(res, con, "soil", "afsis_county", expect=None, essential=False)
    if con is not None and af is not None:
        res.info("soil.afsis_county", f"{af} counties (sentinel validation set, not 47)")

    # iSDA micronutrient columns
    if con is not None and isda:
        for col in ("p_isda", "k_isda", "zn_isda", "fe_isda"):
            coverage_check(res, con, "soil", "isda_county", col, expect=47)

    # soil-index readiness. The definitive signal is the built analytical table
    # (which holds the depth-combined soc / nitrogen / cec and the iSDA columns);
    # otherwise fall back to checking the raw inputs are reachable. SoilGrids
    # properties live under depth-suffixed names (for example soc_0-5cm_mean),
    # so the raw check matches by stem rather than by the post-combination name.
    soil_index_ok = False
    at = base / "analysis" / "outputs" / "county_analytical_table.csv"
    if at.exists():
        import csv
        with open(at, encoding="utf-8") as fh:
            header = next(csv.reader(fh), [])
        if "soil_index" in header:
            res.ok("soil index", "built in county_analytical_table")
            soil_index_ok = True
    if not soil_index_ok and con is not None and sg and isda:
        sg_cols = [c.lower() for c in columns(con, "soil", "soilgrids_zonal_county")]
        isda_cols = set(columns(con, "soil", "isda_county"))
        missing = [s for s in ("soc", "nitrogen", "cec")
                   if not any(s in c for c in sg_cols)]
        missing += [s for s in ("p_isda", "k_isda", "zn_isda", "fe_isda")
                    if s not in isda_cols]
        if not missing:
            res.ok("soil index inputs", "all 7 reachable (SoilGrids stems + iSDA)")
        else:
            res.warn("soil index inputs", f"missing {', '.join(missing)}")

    provenance_summary(res, con, "soil")
    if con is not None:
        con.close()
    return res


def main() -> None:
    run().print_report()


if __name__ == "__main__":
    main()
