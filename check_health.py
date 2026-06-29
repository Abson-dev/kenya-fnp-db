#!/usr/bin/env python3
"""Layer 4 (Body / Health) check.

Verifies the KDHS 2022 and 2014 county anthropometry, the diet and household
controls, the World Bank HNP panel, and that the 2014-to-2022 trend can be
computed. Read-only; needs no rebuild.

Run from the project root:  python check_health.py
"""
from __future__ import annotations

from pathlib import Path

from kenyadb.checks import (CheckResult, columns, connect, coverage_check,
                            has_table, provenance_summary, table_check)

BASE = Path(__file__).resolve().parent
CONTROL_COLS = ["mdd", "wealth_factor_mean", "edu_years_mean", "improved_water_share",
                "improved_sanitation_share", "diarrhea_share", "vit_a_supp_share",
                "maternal_bmi_mean"]


def run(base: Path = BASE) -> CheckResult:
    res = CheckResult("Body / Health")
    con = connect(base)

    # 2022 round: anthropometry and controls
    k22 = table_check(res, con, "health", "kdhs_county", expect=47)
    if con is not None and k22:
        for col in ("stunting", "wasting", "underweight"):
            coverage_check(res, con, "health", "kdhs_county", col, expect=47)
    c22 = table_check(res, con, "health", "kdhs_controls_county", expect=47)
    if con is not None and c22:
        present = [c for c in CONTROL_COLS if c in columns(con, "health", "kdhs_controls_county")]
        res.ok("2022 controls", f"{len(present)}/{len(CONTROL_COLS)} present") \
            if len(present) >= 6 else res.warn("2022 controls", f"only {len(present)} present")

    # 2014 round: the second time point
    k14 = table_check(res, con, "health", "kdhs_county_2014", expect=47, essential=False)
    table_check(res, con, "health", "kdhs_controls_county_2014", expect=47, essential=False)
    if con is not None and k14:
        coverage_check(res, con, "health", "kdhs_county_2014", "stunting", expect=47, essential=False)
    elif con is not None and k14 is None:
        res.info("KDHS 2014", "not built (place the phase-72 recodes to add the trend)")

    # trend readiness
    if con is not None and k22 and k14:
        res.ok("stunting trend 2014 to 2022", "both rounds present, change computable")

    # anaemia is genuinely absent in both Kenya DHS rounds
    res.info("anaemia", "not measured in either KDHS round (MIS only, not county-representative)")

    # national HNP panel
    table_check(res, con, "health", "wb_hnp_panel", expect=None, essential=False)

    # DHS GPS clusters (optional; for the child-level multilevel model)
    for tbl, label in (("kdhs_gps_clusters", "2022"), ("kdhs_gps_clusters_2014", "2014")):
        if con is not None and has_table(con, "health", tbl):
            from kenyadb.checks import count as _count
            res.ok(f"DHS GPS clusters {label}", f"{_count(con, 'health', tbl)} geocoded clusters")
        else:
            res.info(f"DHS GPS clusters {label}", "not built (optional; place the GPS shapefile)")

    provenance_summary(res, con, "health")
    if con is not None:
        con.close()
    return res


def main() -> None:
    run().print_report()


if __name__ == "__main__":
    main()
