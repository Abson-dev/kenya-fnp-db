#!/usr/bin/env python3
"""Run the starter analysis on the Kenya FNP database.

  python analyze.py                 # uses data/db/kenya_fnp.duckdb
  python analyze.py --db path.duckdb

Outputs (analysis/outputs/):
  county_analytical_table.csv   one row per county (soil, prices, population,
                                agriculture, crop production, derived indicators)
  table1_descriptives.csv       summary statistics
  soil_typology.csv             county soil-health zones
  napr_crop_yields.csv          county x crop area, production and yield (t/ha)
  napr_national_crop_summary.csv national crop area / production / yield by year
  kdhs_county_nutrition.csv     county stunting, wasting, underweight, anaemia (KDHS 2022)
  kdhs_vs_actionplan_stunting.csv survey stunting vs the four Action Plan figures
  soil_price_model.txt          exploratory price ~ soil regression
  soil_yield_model.txt          exploratory maize-yield ~ soil regression
  soil_nutrition_model.txt      legacy soil-only stunting regression (price-limited)
  stage1_food_density_model.txt Stage 1: food nutrient density ~ soil index + controls (n up to 47)
  stage2_nutrition_model.txt    Stage 2: stunting ~ food density + soil index + wealth/education/WASH (n=47)
  map_*.png                     choropleths (soil, prices, yield, land use, density,
                                stunting, anaemia, zones)
  METHODS.md                    short methods note for the manuscript

Author: Aboubacar HEMA
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")  # silence the KMeans MKL warning on Windows

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
    if "soil_index" in table.columns:
        used = [c for c in A.SOIL_INDEX_POS if c in table.columns]
        micro = [c for c in ("p_isda", "k_isda", "zn_isda", "fe_isda") if c in table.columns]
        tag = " (incl. iSDA micronutrients)" if micro else ""
        print(f"  -> soil_index built from {len(used)} properties{tag}: {used}")

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
    if "policy_signal_index" in table.columns:
        prio = table.get("fertilizer_priority")
        if prio is not None:
            n23 = int((prio >= 1).sum())
            print(f"  -> policy panel: fertilizer rollout merged ({n23} counties by 2023), "
                  "Policy Signal Index built")
        s4, s4u = A.stage4_policy_response_model(table)
        if s4 is not None:
            (OUT / "stage4_policy_response_model.txt").write_text(str(s4.summary()), encoding="utf-8")
            print(f"  -> Stage 4 policy signal on [{', '.join(s4u)}]; n={int(s4.nobs)}, "
                  f"R2={s4.rsquared:.3f}")
        else:
            print("  -> Stage 4 skipped (need the policy signal plus nutrient gaps)")
    else:
        print("  -> no policy panel in the database (run: python run_all.py --layer policy)")

    print("[analysis] food: crop area, production and yields (NAPR)")
    yields = A.napr_crop_yields(con)
    if not yields.empty:
        yields.round(3).to_csv(OUT / "napr_crop_yields.csv", index=False)
        print(f"  -> county x crop yields: {yields['crop'].nunique()} crops, "
              f"{yields['county_name'].nunique()} counties")
    natl = A.national_crop_summary(con)
    if not natl.empty:
        natl.to_csv(OUT / "napr_national_crop_summary.csv", index=False)
        print(f"  -> national crop summary: {natl['crop'].nunique()} crops, "
              f"{natl['year'].nunique()} years")
    if yields.empty and "crop_production_mt" not in table.columns:
        print("  -> no NAPR crop table in the database (build the food layer)")

    print("[analysis] exploratory soil-yield model")
    ym = A.soil_yield_model(table)
    if ym is not None:
        (OUT / "soil_yield_model.txt").write_text(str(ym.summary()), encoding="utf-8")
        print(f"  -> OLS maize yield on soil, n={int(ym.nobs)}, R2={ym.rsquared:.3f}")
    else:
        print("  -> skipped (need maize yield + soil columns, or statsmodels)")

    print("[analysis] pathway Stage 1: food nutrient density on soil")
    s1, s1u = A.stage1_food_density_model(table)
    if s1 is not None:
        (OUT / "stage1_food_density_model.txt").write_text(str(s1.summary()), encoding="utf-8")
        print(f"  -> OLS food density on [{', '.join(s1u)}]; n={int(s1.nobs)}, "
              f"R2={s1.rsquared:.3f}")
    else:
        print("  -> skipped (need food_nutrient_density_index + soil_index; "
              "run the food layer and ensure iSDA/SoilGrids are built)")

    print("[analysis] health: KDHS 2022 county nutrition (soil-to-nutrition pathway)")
    nut_cols = [c for c in ("stunting", "wasting", "underweight", "child_anaemia",
                            "women_anaemia") if c in table.columns]
    if nut_cols:
        keep = ["county_code", "county_name"] + nut_cols + [
            c for c in ("n_children", "n_women") if c in table.columns]
        table[keep].dropna(subset=nut_cols, how="all").to_csv(
            OUT / "kdhs_county_nutrition.csv", index=False)
        print(f"  -> KDHS county nutrition: {', '.join(nut_cols)}")
        s2, s2u = A.stage2_nutrition_model(table, "stunting")
        if s2 is not None:
            (OUT / "stage2_nutrition_model.txt").write_text(str(s2.summary()), encoding="utf-8")
            print(f"  -> Stage 2 stunting on [{', '.join(s2u)}]; n={int(s2.nobs)}, "
                  f"R2={s2.rsquared:.3f}")
        else:
            print("  -> Stage 2 skipped (need food density + soil index + KDHS controls)")
        nm = A.soil_nutrition_model(table, "stunting")
        if nm is not None:
            (OUT / "soil_nutrition_model.txt").write_text(str(nm.summary()), encoding="utf-8")
            print(f"  -> legacy soil-only model (price-limited), n={int(nm.nobs)}, "
                  f"R2={nm.rsquared:.3f}")
        vac = A.kdhs_vs_actionplan(table)
        if not vac.empty:
            vac.to_csv(OUT / "kdhs_vs_actionplan_stunting.csv", index=False)
            print(f"  -> KDHS vs Action Plan stunting for {len(vac)} named counties")
        if "stunting_2014" in table.columns:
            n14 = int(table["stunting_2014"].notna().sum())
            an = [c for c in ("child_anaemia_2014", "women_anaemia_2014")
                  if c in table.columns and table[c].notna().any()]
            print(f"  -> KDHS 2014 merged: stunting_2014 {n14}/47"
                  + (f"; anaemia from 2014: {', '.join(an)}" if an else ""))
            if "stunting_change" in table.columns and table["stunting_change"].notna().any():
                ch = table["stunting_change"].mean()
                print(f"  -> stunting trend 2014 to 2022: mean change {ch:+.1f} pp "
                      f"({int((table['stunting_change'] < 0).sum())} counties improved)")
            s5, s5u = A.stage5_persistence_model(table)
            if s5 is not None:
                (OUT / "stage5_persistence_model.txt").write_text(str(s5.summary()), encoding="utf-8")
                print(f"  -> Stage 5 (lagged outcome) 2022 stunting on [{', '.join(s5u)}]; "
                      f"n={int(s5.nobs)}, R2={s5.rsquared:.3f}")
    else:
        print("  -> no KDHS county table yet (place the recodes in "
              "data/external/kdhs_2022/ and rebuild)")

    if not args.no_maps:
        print("[analysis] maps")
        gdf = A.county_geometry(BASE)
        if gdf is not None:
            specs = [
                ("soc", "Topsoil organic carbon (g/kg), 0-30 cm", "map_soc.png", "YlGn"),
                ("phh2o", "Topsoil pH (H2O), 0-30 cm", "map_ph.png", "RdYlBu"),
                ("maize_price_median", "Median maize price (KES/kg)", "map_maize_price.png", "YlOrRd"),
                ("maize_price_cv", "Maize price volatility (CV)", "map_maize_volatility.png", "OrRd"),
                ("maize_yield_t_ha", "Maize yield (production / area, t/ha)", "map_maize_yield.png", "YlGn"),
                ("maize_production_per_capita_kg", "Maize production per capita (kg)", "map_maize_pc.png", "BuGn"),
                ("ag_land_share", "Agricultural land as share of county area", "map_ag_land_share.png", "Greens"),
                ("farming_hh_share", "Farming households as share of all households", "map_farming_hh.png", "Purples"),
                ("density", "Population density (persons / sq km)", "map_density.png", "PuBu"),
                ("rain_mm_mean", "Mean annual rainfall (mm), CHIRPS", "map_rainfall.png", "YlGnBu"),
                ("drought_freq", "Drought frequency (share of years below -1 SD)", "map_drought.png", "OrRd"),
                ("ndvi_mean", "Mean NDVI (vegetation greenness), MODIS", "map_ndvi.png", "YlGn"),
                ("ndvi_trend", "NDVI trend (greening +, browning -)", "map_ndvi_trend.png", "RdYlGn"),
                ("stunting", "Child stunting (%), KDHS 2022", "map_stunting.png", "OrRd"),
                ("child_anaemia", "Child anaemia (%), KDHS 2022", "map_child_anaemia.png", "Reds"),
                ("child_anaemia_2014", "Child anaemia (%), KDHS 2014", "map_child_anaemia_2014.png", "Reds"),
                ("stunting_change", "Stunting change 2014 to 2022 (pp)", "map_stunting_change.png", "RdYlGn_r"),
                ("soil_index", "Composite soil-health index (z-score)", "map_soil_index.png", "YlGn"),
                ("zn_isda", "Extractable zinc (iSDA), 0-30 cm", "map_zinc.png", "YlGnBu"),
                ("food_nutrient_density_index", "Food Nutrient Density Index (z-score)", "map_food_density.png", "BuGn"),
                ("mdd", "Minimum dietary diversity, 6-23 mo (%)", "map_mdd.png", "PuBuGn"),
                ("soil_zone", "Soil-health zone", "map_soil_zone.png", "Set2"),
                ("policy_signal_index", "Policy Signal Index (z-score)", "map_policy_signal.png", "BuPu"),
                ("fertilizer_priority", "Fertilizer subsidy rollout priority", "map_fertilizer.png", "Greens"),
            ]
            src = typed if "soil_zone" in typed.columns else table
            for col, title, fn, cmap in specs:
                if col in src.columns:
                    if A.choropleth(gdf, src, col, title, OUT / fn, cmap=cmap) is not None:
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
        "g/kg, cation exchange capacity in cmol(c)/kg). Extractable micronutrients "
        "(phosphorus, potassium, zinc, iron) are added from iSDAsoil, a continental "
        "gridded prediction, by the same county zonal-statistics method, and a "
        "composite soil-health index is formed as the z-score average across "
        "counties of the fertility-positive properties (organic carbon, nitrogen, "
        "cation exchange capacity and the iSDA micronutrients).\n\n"
        "## Prices\n\n"
        "Retail staple prices from the World Food Programme are normalised to a "
        "per-kilogram basis, then summarised per county as the median price "
        "level and the coefficient of variation as a volatility measure.\n\n"
        "## Population and agriculture denominators\n\n"
        "County population, households, average household size, land area and "
        "density come from the 2019 Kenya Population and Housing Census. "
        "Agricultural land (hectares) and farming households, split by "
        "subsistence and commercial purpose, come from the census agriculture "
        "tables. These denominators support per-capita and land-use-intensity "
        "indicators: agricultural land as a share of county area, farming "
        "households as a share of all households, and cropland per farming "
        "household.\n\n"
        "## Crop area, production and yields\n\n"
        "Crop area and production by county and year (2019-2023) are extracted "
        "from the KNBS National Agriculture Production Report 2024. For each "
        "county the latest year gives total cropped area, total production and "
        "the number of crops reported; maize is retained separately as the "
        "staple. County maize yield is production divided by area (t/ha), and "
        "maize production per capita uses the census population. Crop area, "
        "production and yield are also reported nationally by crop and year.\n\n"
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
        "## Health and nutrition outcomes\n\n"
        "County child anthropometry (stunting, wasting, underweight) comes from "
        "the 2022 Kenya Demographic and Health Survey. Prevalence is computed "
        "per county from the children (KR) recode, sample-weighted by v005/1e6, "
        "with z-score flags excluded (|z| beyond 6 SD). The county variable is "
        "detected by matching its value labels to the crosswalk. The 2022 KDHS "
        "biomarker questionnaire collected height and weight only; unlike the "
        "2014 round it carried no haemoglobin module, so anaemia is not "
        "available from this survey and is omitted rather than reported empty. "
        "Child stunting is the outcome for the soil-food-body pathway. Two stages "
        "are estimated. Stage 1 regresses the county Food Nutrient Density Index, "
        "built by mapping crop production to nutrients through the Kenya Food "
        "Composition Tables and expressing it per capita, on the composite "
        "soil-health index with land and climate controls. Stage 2 regresses child "
        "stunting on the food nutrient density index and the soil index, "
        "controlling for household wealth, maternal education and water and "
        "sanitation from the same survey. Both stages estimate at full county "
        "coverage, replacing an earlier price-limited model that ran on fewer "
        "counties. Minimum dietary diversity for children 6 to 23 months is also "
        "computed from the survey as a complementary diet-quality measure. "
        "The four county stunting figures the Action Plan names "
        "(Kilifi, West Pokot, Samburu, Kisumu) are compared against the survey "
        "estimates as an external validation; Kilifi at about 37 percent matches "
        "the Action Plan figure closely.\n\n"
        "## Causal caution\n\n"
        "All county associations reported here are descriptive. Soil quality, "
        "climate, market access, diets and care practices are jointly "
        "determined, so the soil-price, soil-yield and soil-nutrition models "
        "are read as conditional gradients with robust (HC3) standard errors, "
        "not as causal effects. The Stage 1 and Stage 2 pathway models are read "
        "the same way; and because the soil layer is a single cross-section, soil "
        "effects are identified between counties rather than within them.\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
