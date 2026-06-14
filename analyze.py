#!/usr/bin/env python3
"""Run the starter analysis on the Kenya FNP database.

  python analyze.py                 # uses data/db/kenya_fnp.duckdb
  python analyze.py --db path.duckdb

Outputs (analysis/outputs/):
  county_analytical_table.csv   one row per county (soil + staple prices)
  table1_descriptives.csv       summary statistics
  soil_typology.csv             county soil-health zones
  map_*.png                     choropleths (SOC, pH, maize price, volatility, zones)
  soil_price_model.txt          exploratory regression summary
  METHODS.md                    short methods note for the manuscript

Author: Aboubacar HEMA
"""
from __future__ import annotations

import argparse
from pathlib import Path

from kenyadb import analysis as A

BASE = Path(__file__).resolve().parent
DB = BASE / "data" / "db" / "kenya_fnp.duckdb"
OUT = BASE / "analysis" / "outputs"


def main() -> None:
    ap = argparse.ArgumentParser(description="Starter analysis for the Kenya FNP database")
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--no-maps", action="store_true", help="skip choropleth maps")
    args = ap.parse_args()
    if not args.db.exists():
        raise SystemExit(f"database not found: {args.db} (run run_all.py first)")
    OUT.mkdir(parents=True, exist_ok=True)
    con = A.load_db(args.db)

    print("[analysis] building county analytical table")
    table = A.build_county_table(con)
    table.to_csv(OUT / "county_analytical_table.csv", index=False)
    print(f"  -> {len(table)} counties, {table.shape[1]} columns")

    print("[analysis] descriptive statistics")
    A.descriptive_stats(table).to_csv(OUT / "table1_descriptives.csv")

    print("[analysis] soil-health typology")
    typed, meta, feats = A.soil_typology(table)
    typed[["county_code", "county_name", "soil_zone"]].to_csv(OUT / "soil_typology.csv", index=False)
    if meta:
        print(f"  -> k={meta['k']} zones (silhouette={meta['silhouette']}), features={feats}")

    print("[analysis] exploratory soil-price model")
    model = A.soil_price_model(table)
    if model is not None:
        (OUT / "soil_price_model.txt").write_text(str(model.summary()), encoding="utf-8")
        print(f"  -> OLS on n={int(model.nobs)} counties, R2={model.rsquared:.3f}")
    else:
        print("  -> skipped (need maize price + soil columns, or statsmodels)")

    print("[analysis] policy layer (Action Plan)")
    budget = A.policy_budget_by_transition(con)
    if not budget.empty:
        budget.to_csv(OUT / "policy_budget_by_transition.csv", index=False)
        A.barh(budget, "critical_transition", "cost_kes_millions",
               "Action Plan 2024-2030 budget by critical transition",
               OUT / "policy_budget_by_transition.png", xlabel="KES millions")
        print(f"  -> budget by transition ({len(budget)} transitions, "
              f"total {budget['cost_kes_millions'].sum():,.0f} KES m)")
    growth = A.policy_ag_growth(con)
    if not growth.empty:
        growth.to_csv(OUT / "policy_ag_growth.csv", index=False)
        print(f"  -> agricultural growth series ({len(growth)} years)")
    cross = A.policy_nutrition_cross_check(table)
    if not cross.empty:
        cross.round(2).to_csv(OUT / "policy_nutrition_vs_soil.csv", index=False)
        print(f"  -> Action Plan stunting vs soil/price for {len(cross)} named "
              "counties (illustrative external check)")
    elif "stunting_actionplan" not in table.columns:
        print("  -> no policy county nutrition in the database (build the policy layer)")

    if not args.no_maps:
        print("[analysis] maps")
        gdf = A.county_geometry(BASE)
        if gdf is not None:
            specs = [
                ("soc", "Topsoil organic carbon (g/kg), 0-30 cm", "map_soc.png", "YlGn"),
                ("phh2o", "Topsoil pH (H2O), 0-30 cm", "map_ph.png", "RdYlBu"),
                ("maize_price_median", "Median maize price (KES/kg)", "map_maize_price.png", "YlOrRd"),
                ("maize_price_cv", "Maize price volatility (CV)", "map_maize_volatility.png", "OrRd"),
                ("soil_zone", "Soil-health zone", "map_soil_zone.png", "Set2"),
            ]
            src = typed if "soil_zone" in typed.columns else table
            for col, title, fn, cmap in specs:
                if col in src.columns:
                    A.choropleth(gdf, src, col, title, OUT / fn, cmap=cmap)
                    print(f"  -> {fn}")
        else:
            print("  -> no county geometry found; skipping maps")

    _write_methods(OUT / "METHODS.md", table, meta)
    con.close()
    print(f"[analysis] done -> {OUT}")


def _write_methods(path: Path, table, meta) -> None:
    k = meta["k"] if meta else "k"
    path.write_text(
        "# Methods note (draft)\n\n"
        "Author: Aboubacar HEMA\n\n"
        "## Spatial unit and linkage\n\n"
        "All indicators are resolved to Kenya's 47 counties using a master "
        "county and sub-county crosswalk derived from the OCHA Common "
        "Operational Dataset administrative boundaries. Thematic layers are "
        "appended to this crosswalk rather than merged pairwise, preserving a "
        "single spatial key. Market price observations, which the World Food "
        "Programme tags by former province, are assigned to counties by "
        "point-in-polygon on market coordinates.\n\n"
        "## Soil\n\n"
        "County soil properties are zonal means of ISRIC SoilGrids 250 m "
        "coverages. The 0-5, 5-15 and 15-30 cm layers are combined into a "
        "thickness-weighted 0-30 cm topsoil value and converted from SoilGrids "
        "mapped units to conventional units (for example pH, organic carbon in "
        "g/kg, cation exchange capacity in cmol(c)/kg).\n\n"
        "## Prices\n\n"
        "Retail staple prices from the World Food Programme are normalised to a "
        "per-kilogram basis, then summarised per county as the median price "
        "level and the coefficient of variation as a volatility measure.\n\n"
        "## Typology\n\n"
        f"Counties are grouped into soil-health zones by k-means on standardised "
        f"topsoil properties, with the number of zones (k={k}) selected by the "
        "silhouette criterion.\n\n"
        "## Policy layer\n\n"
        "The Food Systems and Land Use Action Plan 2024-2030 contributes national "
        "policy context and a small county signal. The 7-year budget is summarised "
        "by critical transition, and the agricultural growth series is retained as "
        "context. The Plan also names four counties with a stunting figure "
        "(Kilifi, West Pokot, Samburu, Kisumu); these are joined to the county "
        "table and placed beside the soil and price profile as an external, "
        "illustrative check. With four counties this is not an inferential "
        "analysis and is not used as a model input.\n\n"
        "## Planned extension\n\n"
        "County anthropometry and anaemia from the 2022 Kenya Demographic and "
        "Health Survey will be appended to the county table to estimate the soil "
        "to nutrition pathway, with the four Action Plan county figures serving as "
        "an external check on the survey aggregates. Current soil-price "
        "associations are descriptive: soil quality, market access and prices are "
        "jointly determined, so they are reported with robust standard errors and "
        "not interpreted causally.\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
