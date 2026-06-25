#!/usr/bin/env python3
"""Layer 1 (Geography, denominators and environment) check.

Verifies the master crosswalk, the census denominators, and the remote-sensing
covariate layer (rainfall, NDVI, drought). Read-only; needs no rebuild.

Run from the project root:  python check_geography.py
"""
from __future__ import annotations

from pathlib import Path

from kenyadb.checks import CheckResult, connect, table_check, coverage_check

BASE = Path(__file__).resolve().parent


def run(base: Path = BASE) -> CheckResult:
    res = CheckResult("Geography, denominators and environment")
    con = connect(base)

    # processed spine on disk
    cw = base / "data" / "processed" / "crosswalk_admin.csv"
    res.ok("processed/crosswalk_admin.csv", "present") if cw.exists() \
        else res.warn("processed/crosswalk_admin.csv", "missing (run the crosswalk step)")

    # crosswalk spine in the database (290 sub-counties, 47 counties)
    n = table_check(res, con, "core", "crosswalk_admin", expect=290)
    if con is not None and n is not None:
        counties = con.execute(
            "select count(distinct county_norm) from core.crosswalk_admin").fetchone()[0]
        (res.ok if counties == 47 else res.warn)("crosswalk counties", f"{counties}/47")

    # census denominators
    table_check(res, con, "geography", "census_population_county", expect=47)
    table_check(res, con, "geography", "census_agriculture_county", expect=47)
    table_check(res, con, "geography", "census_population_subcounty", expect=None)
    table_check(res, con, "geography", "census_agriculture_subcounty", expect=None)

    # environment: remote sensing (optional GEE layer)
    rs = table_check(res, con, "geography", "remote_sensing_county",
                     expect=47, essential=False)
    if con is not None and rs is not None:
        for col in ("rain_mm_mean", "ndvi_mean", "drought_freq"):
            coverage_check(res, con, "geography", "remote_sensing_county", col,
                           expect=47, essential=False)
    elif con is not None and rs is None:
        res.info("remote sensing", "not built (optional; needs the CHIRPS / MODIS rasters)")

    if con is not None:
        con.close()
    return res


def main() -> None:
    run().print_report()


if __name__ == "__main__":
    main()
