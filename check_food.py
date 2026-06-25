#!/usr/bin/env python3
"""Layer 3 (Food) check.

Verifies FAOSTAT, observed prices, the food composition tables and the NAPR
crop tables, and that the Food Nutrient Density Index has the inputs it needs.
Read-only; needs no rebuild.

Run from the project root:  python check_food.py
"""
from __future__ import annotations

from pathlib import Path

from kenyadb.checks import (CheckResult, connect, count, has_table,
                            provenance_summary, table_check)

BASE = Path(__file__).resolve().parent


def run(base: Path = BASE) -> CheckResult:
    res = CheckResult("Food")
    con = connect(base)

    # core food tables in the database
    table_check(res, con, "food", "faostat_kenya", expect=None)
    prices = table_check(res, con, "food", "prices_wfp_observed", expect=None)
    napr = table_check(res, con, "food", "napr_crop_county", expect=None)
    for t in ("kfct_foods", "kfct_proximates", "kfct_minerals", "kfct_vitamins"):
        table_check(res, con, "food", t, expect=None)
    # modelled prices are an optional manual source; absence is not a problem
    if con is not None:
        if has_table(con, "food", "prices_wb_modeled") and count(con, "food", "prices_wb_modeled") > 0:
            res.ok("food.prices_wb_modeled", f"{count(con, 'food', 'prices_wb_modeled')} rows")
        else:
            res.info("food.prices_wb_modeled", "optional modelled prices not downloaded")

    # NAPR crop coverage (expect 9 to 10 crops)
    if con is not None and napr:
        crops = con.execute(
            "select count(distinct crop) from food.napr_crop_county").fetchone()[0]
        counties = con.execute(
            "select count(distinct county_norm) from food.napr_crop_county").fetchone()[0]
        (res.ok if crops >= 9 else res.warn)("NAPR crops", f"{crops} crops across {counties} counties")

    # WFP price county coverage (partial is expected)
    if con is not None and prices and "county_norm" in [
            c[0] for c in con.execute(
                "select * from food.prices_wfp_observed limit 0").description]:
        pc = con.execute(
            "select count(distinct county_norm) from food.prices_wfp_observed "
            "where county_norm is not null").fetchone()[0]
        res.info("WFP price coverage", f"{pc}/47 counties (partial by design)")

    # FNDI readiness: NAPR crops plus at least one KFCT composition table
    if con is not None:
        kfct_ok = any(has_table(con, "food", t) and count(con, "food", t) > 0
                      for t in ("kfct_proximates", "kfct_minerals", "kfct_vitamins"))
        if napr and kfct_ok:
            res.ok("FNDI inputs", "NAPR crops and KFCT composition present")
        else:
            res.warn("FNDI inputs", "need both NAPR crop production and a KFCT table")

    provenance_summary(res, con, "food")
    if con is not None:
        con.close()
    return res


def main() -> None:
    run().print_report()


if __name__ == "__main__":
    main()
