"""Analysis layer for the Kenya soil / food / nutrition / policy database.

Reads the assembled DuckDB database and produces a county-level analytical
dataset plus the first set of publication-oriented outputs:

  1. county analytical table (one row per county): topsoil (0-30 cm) soil
     properties in conventional units, and WFP staple-price level + volatility
  2. descriptive statistics (Table 1)
  3. soil-health typology (k-means county clustering, k chosen by silhouette)
  4. price geography (county median price and volatility, mapped)
  5. an exploratory, explicitly associational soil-price model (scaffold)
  6. policy context from the Action Plan (budget by critical transition,
     agricultural growth) and an illustrative county cross-check of the four
     stunting figures the Plan names against the soil and price layers

The soil-to-nutrition pathway that motivates the bundle needs county nutrition
outcomes from KDHS 2022, which is still a pending manual gate; build_county_table
leaves a clear slot for those columns, and the regression scaffold documents the
intended specification so it can be estimated as soon as the survey lands.

Author: Aboubacar HEMA
"""
from __future__ import annotations

import re
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

# SoilGrids v2.0 mapped -> conventional units (divide mapped value by `div`).
SOILGRIDS = {
    "phh2o":    {"div": 10,  "unit": "pH",         "label": "pH (H2O)"},
    "soc":      {"div": 10,  "unit": "g/kg",       "label": "Soil organic carbon"},
    "nitrogen": {"div": 100, "unit": "g/kg",       "label": "Total nitrogen"},
    "cec":      {"div": 10,  "unit": "cmol(c)/kg", "label": "Cation exchange capacity"},
    "bdod":     {"div": 100, "unit": "g/cm3",      "label": "Bulk density"},
    "cfvo":     {"div": 10,  "unit": "vol%",       "label": "Coarse fragments"},
    "clay":     {"div": 10,  "unit": "%",          "label": "Clay"},
    "sand":     {"div": 10,  "unit": "%",          "label": "Sand"},
    "silt":     {"div": 10,  "unit": "%",          "label": "Silt"},
}
# Topsoil depths and their thicknesses (mm-equivalent weights) for a 0-30 cm mean.
TOPSOIL = {"0-5cm": 5, "5-15cm": 10, "15-30cm": 15}
_COVERAGE_RE = re.compile(r"^(?P<prop>[a-z0-9]+)_(?P<depth>\d+-\d+cm)_mean$")
# Fertility-positive soil properties (higher = better) for the composite soil
# index; the iSDA micronutrients are folded in when present so the index reflects
# extractable P, K, Zn and Fe, not only the SoilGrids macro-properties.
SOIL_INDEX_POS = ["soc", "nitrogen", "cec", "p_isda", "k_isda", "zn_isda", "fe_isda"]


def _add_soil_index(t: pd.DataFrame) -> pd.DataFrame:
    """Composite soil-health index: the z-score average across counties of the
    fertility-positive soil properties present (SoilGrids carbon, nitrogen and
    CEC plus the iSDA micronutrients). Higher means more fertile. Descriptive."""
    present = [c for c in SOIL_INDEX_POS if c in t.columns]
    if len(present) < 3:
        return t
    z = t[present].apply(lambda s: (s - s.mean()) / s.std(ddof=0)
                         if s.std(ddof=0) else s * 0.0)
    t = t.copy()
    t["soil_index"] = z.mean(axis=1)
    return t


def _add_gaps(t: pd.DataFrame) -> pd.DataFrame:
    """Nutrient gaps relative to the national (cross-county) mean, oriented so a
    larger value always means a worse position. These are the G terms the Stage 4
    policy-response model uses. gap_body uses stunting (higher = worse); gap_food
    and gap_soil are the shortfall of food density and the soil index below the
    national mean (higher = worse)."""
    t = t.copy()
    if "stunting" in t.columns:
        t["gap_body_stunting"] = t["stunting"] - t["stunting"].mean()
    if "food_nutrient_density_index" in t.columns:
        t["gap_food_density"] = t["food_nutrient_density_index"].mean() - t["food_nutrient_density_index"]
    if "soil_index" in t.columns:
        t["gap_soil"] = t["soil_index"].mean() - t["soil_index"]
    return t


def load_db(db_path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(db_path), read_only=True)


# --------------------------------------------------------------------------
# soil: aggregate SoilGrids depths to a 0-30 cm topsoil mean, convert units
# --------------------------------------------------------------------------
def soil_topsoil(con) -> pd.DataFrame:
    df = con.execute("select * from soil.soilgrids_zonal_county").df()
    keys = [c for c in df.columns if c in ("county_name", "county_norm")]
    out = df[keys].copy()
    # group coverage columns by property
    by_prop: dict[str, list[tuple[str, str]]] = {}
    for c in df.columns:
        m = _COVERAGE_RE.match(c)
        if m and m["depth"] in TOPSOIL and m["prop"] in SOILGRIDS:
            by_prop.setdefault(m["prop"], []).append((c, m["depth"]))
    for prop, cols in by_prop.items():
        w = np.array([TOPSOIL[d] for _, d in cols], dtype=float)
        vals = df[[c for c, _ in cols]].to_numpy(dtype=float)
        wmean = np.nansum(vals * w, axis=1) / w.sum()
        out[prop] = wmean / SOILGRIDS[prop]["div"]
    return out


# --------------------------------------------------------------------------
# food: per-county WFP staple price level and volatility
# --------------------------------------------------------------------------
def _per_kg(price: pd.Series, unit: pd.Series) -> pd.Series:
    """Normalise price to per-kg using the leading number in the unit string
    (e.g. '90 KG' -> divide by 90; 'KG' -> as is)."""
    qty = unit.astype(str).str.extract(r"(\d+(?:\.\d+)?)")[0].astype(float)
    qty = qty.where(qty.notna() & (qty > 0), 1.0)
    return pd.to_numeric(price, errors="coerce") / qty


def wfp_price_summary(con, commodity: str = "Maize", pricetype: str = "Retail",
                      currency: str = "KES") -> pd.DataFrame:
    df = con.execute("select * from food.prices_wfp_observed").df()
    if df.empty:
        return pd.DataFrame()
    low = {c.lower(): c for c in df.columns}
    # filters (only apply those whose columns exist)
    if "commodity" in low:
        df = df[df[low["commodity"]].astype(str).str.contains(commodity, case=False, na=False)]
    if "pricetype" in low:
        df = df[df[low["pricetype"]].astype(str).str.contains(pricetype, case=False, na=False)]
    if "currency" in low and currency:
        df = df[df[low["currency"]].astype(str).str.upper() == currency]
    if df.empty or "county_norm" not in df.columns:
        return pd.DataFrame()
    price = (_per_kg(df[low["price"]], df[low["unit"]]) if "unit" in low
             else pd.to_numeric(df[low["price"]], errors="coerce"))
    df = df.assign(price_kg=price)
    df = df[df["price_kg"].notna() & (df["price_kg"] > 0)]
    g = df.groupby("county_norm")["price_kg"]
    summ = pd.DataFrame({
        "price_kes_kg_median": g.median(),
        "price_kes_kg_mean": g.mean(),
        "price_cv": g.std() / g.mean(),     # volatility
        "price_n_obs": g.size(),
    }).reset_index()
    summ.columns = ["county_norm", f"{commodity.lower()}_price_median",
                    f"{commodity.lower()}_price_mean", f"{commodity.lower()}_price_cv",
                    f"{commodity.lower()}_price_n"]
    return summ


# --------------------------------------------------------------------------
# county analytical master table
# --------------------------------------------------------------------------
def remote_sensing_county(con) -> pd.DataFrame:
    """County rainfall / NDVI / drought covariates, if the layer has been built
    (returns empty when no remote-sensing rasters have been placed yet)."""
    try:
        df = con.execute("select * from geography.remote_sensing_county").df()
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    return df.drop(columns=[c for c in ("county_name", "county_code") if c in df.columns],
                   errors="ignore")


def afsis_county(con) -> pd.DataFrame:
    """County AfSIS extractable micronutrients (P, K, Zn, Fe and others), if the
    layer has been built (returns empty when the AfSIS points are not present)."""
    try:
        df = con.execute("select * from soil.afsis_county").df()
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    return df.drop(columns=[c for c in ("county_name",) if c in df.columns], errors="ignore")


def isda_county(con) -> pd.DataFrame:
    """County iSDAsoil gridded nutrients (P, K, Zn, Fe and more) at full 47-county
    coverage, if the layer has been built. Returns empty when absent."""
    try:
        df = con.execute("select * from soil.isda_county").df()
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    return df.drop(columns=[c for c in ("county_name",) if c in df.columns], errors="ignore")


def kdhs_controls_county(con) -> pd.DataFrame:
    """County dietary diversity (MDD) and food-to-body controls (wealth,
    maternal education, water and sanitation), if the layer has been built."""
    try:
        df = con.execute("select * from health.kdhs_controls_county").df()
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    return df.drop(columns=[c for c in ("county_name",) if c in df.columns], errors="ignore")


def build_county_table(con) -> pd.DataFrame:
    counties = con.execute("""
        select distinct county_code, county_name, county_norm
        from core.crosswalk_admin where county_norm <> ''
    """).df()
    soil = soil_topsoil(con)
    table = counties.merge(soil.drop(columns=["county_name"], errors="ignore"),
                           on="county_norm", how="left")
    # soil vector extension: AfSIS extractable micronutrients (P, K, Zn, Fe ...)
    micro = afsis_county(con)
    if not micro.empty:
        table = table.merge(micro, on="county_norm", how="left")
    # soil vector extension: iSDAsoil gridded nutrients at full 47-county coverage
    isda = isda_county(con)
    if not isda.empty:
        table = table.merge(isda, on="county_norm", how="left")
    prices = wfp_price_summary(con)
    if not prices.empty:
        table = table.merge(prices, on="county_norm", how="left")
    # geography: population denominators and agricultural land / farming households
    pop = census_population(con)
    if not pop.empty:
        table = table.merge(pop, on="county_code", how="left")
    ag = census_agriculture(con)
    if not ag.empty:
        table = table.merge(ag, on="county_code", how="left")
    # food: crop area and production (latest NAPR year), enables yields and per-capita
    crop = napr_crop_summary(con)
    if not crop.empty:
        table = table.merge(crop, on="county_code", how="left")
    # food: Food Nutrient Density Index (F) from NAPR production x KFCT composition
    fnd = food_nutrient_density_county(con)
    if not fnd.empty:
        table = table.merge(fnd.drop(columns=[c for c in ("napr_year",) if c in fnd.columns]),
                            on="county_code", how="left")
    # food: crop-mix diversity (Shannon), maize share and crop count (NAPR)
    cd = crop_diversity_county(con)
    if not cd.empty:
        ckey = "county_code" if "county_code" in cd.columns else "county_norm"
        table = table.merge(cd, on=ckey, how="left")
    # Policy layer: the Action Plan names four counties with a stunting figure.
    # Sparse (4 of 47), so it serves as an external check, not a model input.
    ap = policy_county_nutrition(con)
    if not ap.empty:
        table = table.merge(ap, on="county_code", how="left")
    # Health layer: KDHS 2022 county nutrition outcomes (the analytical centrepiece
    # of the soil-to-nutrition pathway). Joined once the recodes are processed.
    kd = kdhs_county_estimates(con)
    if not kd.empty:
        table = table.merge(kd, on="county_code", how="left")
    # Health layer (second time point): KDHS 2014 anthropometry and anaemia, with
    # the 2014-to-2022 stunting change. The 2014 round supplies the anaemia the
    # 2022 round omits, and the lag that the Stage 5 substitute model needs.
    kd14 = kdhs_2014_estimates(con)
    if not kd14.empty:
        table = table.merge(kd14, on="county_code", how="left")
        if {"stunting", "stunting_2014"} <= set(table.columns):
            table["stunting_change"] = table["stunting"] - table["stunting_2014"]
    # Health layer: dietary diversity (MDD) and food-to-body controls (wealth,
    # maternal education, water and sanitation) from the KDHS children's recode.
    kdc = kdhs_controls_county(con)
    if not kdc.empty:
        table = table.merge(kdc, on="county_code", how="left")
    # Environment layer: remote-sensing covariates (rainfall, NDVI, drought),
    # available once the annual rasters are placed and the layer is built.
    rs = remote_sensing_county(con)
    if not rs.empty:
        table = table.merge(rs, on="county_norm", how="left")
    # policy layer: county fertilizer rollout, expenditure intensity, signal index
    pol = policy_county_summary(con)
    if not pol.empty:
        table = table.merge(pol, on="county_norm", how="left")
    table = _add_derived(table)
    table = _add_soil_index(table)
    table = _add_gaps(table)
    return table.sort_values("county_code").reset_index(drop=True)


# --------------------------------------------------------------------------
# policy layer (Action Plan)
# --------------------------------------------------------------------------
def _has_table(con, schema: str, name: str) -> bool:
    q = ("select count(*) from information_schema.tables "
         "where table_schema = ? and table_name = ?")
    return con.execute(q, [schema, name]).fetchone()[0] > 0


def policy_county_nutrition(con) -> pd.DataFrame:
    """County child-nutrition figures cited in the Action Plan, one column per
    indicator (only stunting is given at county level). Returns an empty frame
    if the policy table is absent."""
    tbl = "action_plan__action_plan_county_nutrition"
    if not _has_table(con, "policy", tbl):
        return pd.DataFrame()
    df = con.execute(f"select county_code, indicator, value_pct from policy.{tbl}").df()
    if df.empty:
        return pd.DataFrame()
    wide = (df.pivot_table(index="county_code", columns="indicator",
                           values="value_pct", aggfunc="first")
              .reset_index())
    wide.columns = ["county_code"] + [f"{c.lower()}_actionplan" for c in wide.columns[1:]]
    return wide


def policy_county_summary(con) -> pd.DataFrame:
    """County policy-intensity cross-section: fertilizer-subsidy rollout timing and
    priority, latest-year agricultural expenditure intensity where present, and the
    composite Policy Signal Index. Empty if the policy panel is not built."""
    if not _has_table(con, "policy", "policy_county_summary"):
        return pd.DataFrame()
    df = con.execute("select * from policy.policy_county_summary").df()
    return df.drop(columns=[c for c in ("county_code", "county_name") if c in df.columns],
                   errors="ignore")


def policy_budget_by_transition(con) -> pd.DataFrame:
    """Action Plan 7-year budget summed by critical transition (KES millions)."""
    tbl = "action_plan__action_plan_budget_7yr"
    if not _has_table(con, "policy", tbl):
        return pd.DataFrame()
    return con.execute(f"""
        select critical_transition,
               round(sum(cost_kes_millions), 1) as cost_kes_millions
        from policy.{tbl}
        group by critical_transition
        order by cost_kes_millions desc
    """).df()


def policy_ag_growth(con) -> pd.DataFrame:
    """Agricultural sector growth series (%) from the Action Plan."""
    tbl = "action_plan__action_plan_ag_growth"
    if not _has_table(con, "policy", tbl):
        return pd.DataFrame()
    return con.execute(
        f"select year, ag_sector_growth_pct from policy.{tbl} order by year").df()


# --------------------------------------------------------------------------
# geography: population and agriculture denominators (2019 KPHC via census.py)
# --------------------------------------------------------------------------
def census_population(con) -> pd.DataFrame:
    """County population denominators: population, households, average household
    size, land area and density."""
    if not _has_table(con, "geography", "census_population_county"):
        return pd.DataFrame()
    df = con.execute("select * from geography.census_population_county").df()
    cols = ["county_code"] + [c for c in (
        "population_total", "households", "avg_household_size",
        "land_area_sqkm", "density") if c in df.columns]
    return df[cols].drop_duplicates("county_code")


def census_agriculture(con) -> pd.DataFrame:
    """County agricultural land (ha) and farming households, total and split by
    subsistence / commercial purpose."""
    if not _has_table(con, "geography", "census_agriculture_county"):
        return pd.DataFrame()
    df = con.execute("select * from geography.census_agriculture_county").df()
    cols = ["county_code"] + [c for c in (
        "ag_land_ha_total", "ag_land_ha_subsistence", "ag_land_ha_commercial",
        "farming_households_total", "farming_households_subsistence",
        "farming_households_commercial") if c in df.columns]
    return df[cols].drop_duplicates("county_code")


def kdhs_county_estimates(con) -> pd.DataFrame:
    """County child anthropometry (stunting, wasting, underweight), child and
    women anaemia from KDHS 2022 (health.kdhs_county). This is the outcome layer
    for the soil-to-nutrition analysis."""
    if not _has_table(con, "health", "kdhs_county"):
        return pd.DataFrame()
    df = con.execute("select * from health.kdhs_county").df()
    keep = ["county_code"] + [c for c in (
        "stunting", "wasting", "underweight", "child_anaemia", "women_anaemia",
        "n_children", "n_women") if c in df.columns]
    return df[keep].drop_duplicates("county_code")


def kdhs_2014_estimates(con) -> pd.DataFrame:
    """County child anthropometry and anaemia from KDHS 2014 (health.kdhs_county_2014),
    suffixed _2014. The 2014 round carries the haemoglobin biomarker, so child and
    women anaemia are available here even though the 2022 round omits them. Empty
    if the 2014 table is not built."""
    if not _has_table(con, "health", "kdhs_county_2014"):
        return pd.DataFrame()
    df = con.execute("select * from health.kdhs_county_2014").df()
    ren = {c: f"{c}_2014" for c in
           ("stunting", "wasting", "underweight", "child_anaemia", "women_anaemia")
           if c in df.columns}
    keep = ["county_code"] + list(ren)
    return df[keep].drop_duplicates("county_code").rename(columns=ren)


# --------------------------------------------------------------------------
# food: NAPR crop area and production (kenyadb.napr -> food.napr_crop_county)
# --------------------------------------------------------------------------
def napr_crop_summary(con, year: int | None = None) -> pd.DataFrame:
    """Per-county crop totals for the latest NAPR year: total cropped area,
    total production, number of crops reported, and maize area / production."""
    if not _has_table(con, "food", "napr_crop_county"):
        return pd.DataFrame()
    df = con.execute("select * from food.napr_crop_county").df()
    if df.empty:
        return pd.DataFrame()
    if year is None:
        year = int(df["year"].max())
    d = df[df["year"] == year].copy()
    agg = d.groupby("county_code").agg(
        crop_area_ha=("area_ha", "sum"),
        crop_production_mt=("production_mt", "sum")).reset_index()
    nc = (d[d["production_mt"].notna()].groupby("county_code")["crop"]
          .nunique().rename("n_crops").reset_index())
    agg = agg.merge(nc, on="county_code", how="left")
    mz = (d[d["crop"].str.lower() == "maize"].groupby("county_code")
          .agg(maize_area_ha=("area_ha", "sum"),
               maize_production_mt=("production_mt", "sum")).reset_index())
    out = agg.merge(mz, on="county_code", how="left")
    out["napr_year"] = year
    return out


def crop_diversity_county(con) -> pd.DataFrame:
    """County crop-mix controls from NAPR: a Shannon diversity index over crop
    production shares, the maize share of production, and the crop count. These
    are the crop-mix controls the Stage 1 specification calls for."""
    if not _has_table(con, "food", "napr_crop_county"):
        return pd.DataFrame()
    df = con.execute("select * from food.napr_crop_county").df()
    if df.empty or "production_mt" not in df.columns or "crop" not in df.columns:
        return pd.DataFrame()
    key = "county_code" if "county_code" in df.columns else "county_norm"
    if key not in df.columns:
        return pd.DataFrame()
    df["production_mt"] = pd.to_numeric(df["production_mt"], errors="coerce")
    yr = pd.to_numeric(df.get("year"), errors="coerce")
    if yr is not None and yr.notna().any():
        df = df[yr == int(yr.max())]
    d = df[df["production_mt"].notna() & (df["production_mt"] > 0)]
    if d.empty:
        return pd.DataFrame()
    rows = []
    for cc, g in d.groupby(key):
        p = g.groupby("crop")["production_mt"].sum()
        tot = float(p.sum())
        if tot <= 0:
            continue
        sh = p / tot
        shannon = float(-(sh * np.log(sh)).sum())
        maize_mask = p.index.to_series().str.contains("maize", case=False, na=False)
        maize_share = float(p[maize_mask.values].sum() / tot) if maize_mask.any() else 0.0
        rows.append({key: cc, "crop_diversity_shannon": round(shannon, 3),
                     "maize_production_share": round(maize_share, 3),
                     "n_crops_county": int(len(p))})
    return pd.DataFrame(rows)


def napr_crop_yields(con, year: int | None = None) -> pd.DataFrame:
    """Tidy crop x county yields (production / area, t/ha) for the latest year."""
    if not _has_table(con, "food", "napr_crop_county"):
        return pd.DataFrame()
    df = con.execute("select * from food.napr_crop_county").df()
    if df.empty:
        return pd.DataFrame()
    if year is None:
        year = int(df["year"].max())
    d = df[df["year"] == year].copy()
    area = pd.to_numeric(d["area_ha"], errors="coerce")
    d["yield_t_ha"] = pd.to_numeric(d["production_mt"], errors="coerce") / area.where(area > 0)
    cols = [c for c in ("county_code", "county_name", "crop", "year",
                        "area_ha", "production_mt", "yield_t_ha") if c in d.columns]
    return d[cols].sort_values(["crop", "county_name"]).reset_index(drop=True)


def national_crop_summary(con) -> pd.DataFrame:
    """National crop area, production and implied yield by crop and year."""
    if not _has_table(con, "food", "napr_crop_county"):
        return pd.DataFrame()
    df = con.execute("""
        select crop, year,
               round(sum(area_ha), 1) as area_ha,
               round(sum(production_mt), 1) as production_mt
        from food.napr_crop_county group by crop, year order by crop, year
    """).df()
    if not df.empty:
        df["yield_t_ha"] = (df["production_mt"] / df["area_ha"].where(df["area_ha"] > 0)).round(3)
    return df


# --------------------------------------------------------------------------
# food: Food Nutrient Density Index (NAPR production x KFCT composition)
# --------------------------------------------------------------------------
# Maps county crop production to per-capita nutrient supply through the food
# composition tables: supply = production x edible fraction x nutrient content
# per gram, summed over matched crops, divided by population and days. This is
# the food nutrient density (F) variable of the empirical strategy. It is a
# production-based availability proxy, not measured consumption.
_FNDI_NUTRIENTS = {            # output key -> KFCT per-100g column
    "energy_kcal":  "energy_kcal",
    "protein_g":    "protein_g",
    "iron_mg":      "fe_mg",
    "zinc_mg":      "zn_mg",
    "vita_rae_mcg": "vit_a_rae_mcg",
    "folate_mcg":   "folate_dfe_mcg",
    "calcium_mg":   "ca_mg",
}
_FNDI_PC_NAME = {
    "energy_kcal": "food_kcal_pc_day", "protein_g": "food_protein_g_pc_day",
    "iron_mg": "food_iron_mg_pc_day", "zinc_mg": "food_zinc_mg_pc_day",
    "vita_rae_mcg": "food_vita_rae_pc_day", "folate_mcg": "food_folate_pc_day",
    "calcium_mg": "food_calcium_mg_pc_day",
}
_FNDI_COMPOSITE = ["protein_g", "iron_mg", "zinc_mg", "vita_rae_mcg", "folate_mcg", "calcium_mg"]
# NAPR crop (normalised) -> (KFCT name tokens any-of, forbidden tokens). norm()
# removes spaces, so "Sweet Potatoes" -> "sweetpotatoes", "Green Grams" -> "greengrams".
_CROP_KFCT = {
    "maize":          (["maize"], ["flour", "green", "baby", "starch"]),
    "beans":          (["bean"], ["green", "leaf", "flour"]),
    "irishpotatoes":  (["potato"], ["sweet", "crisp", "chip"]),
    "potatoes":       (["potato"], ["sweet", "crisp", "chip"]),
    "sweetpotatoes":  (["sweetpotato"], ["leaf"]),
    "sorghum":        (["sorghum"], ["flour"]),
    "millet":         (["millet"], ["flour"]),
    "fingermillet":   (["millet"], ["flour"]),
    "pearlmillet":    (["millet"], ["flour"]),
    "rice":           (["rice"], ["flour"]),
    "wheat":          (["wheat"], ["flour", "bread", "bran"]),
    "greengrams":     (["greengram", "mung"], ["flour"]),
    "cowpeas":        (["cowpea"], ["leaf"]),
    "pigeonpeas":     (["pigeonpea", "pigeon"], []),
    "cassava":        (["cassava"], ["flour", "leaf"]),
    "bananas":        (["banana"], ["juice", "ripe"]),
    "groundnuts":     (["groundnut", "peanut"], ["butter", "oil"]),
    "soybeans":       (["soya", "soybean"], ["oil", "sauce"]),
    "barley":         (["barley"], ["flour"]),
}


def _fndi_nrm(s) -> str:
    return re.sub(r"[^0-9a-z]+", "", str(s).lower())


def _fndi_num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return np.nan


def _match_crop_food(crop_norm: str, foods_norm: dict):
    """Pick the single best KFCT food for a NAPR crop, preferring raw/whole
    staples and rejecting processed or non-food forms."""
    spec = _CROP_KFCT.get(crop_norm)
    anys, forb = spec if spec else ([crop_norm], [])
    cands = [i for i, nm in foods_norm.items()
             if any(a in nm for a in anys) and not any(f in nm for f in forb)]
    if not cands:
        return None

    def score(i):
        nm, s = foods_norm[i], 0
        for good in ("raw", "dried", "dry", "whole", "grain", "mature", "wholemeal"):
            if good in nm:
                s += 2
        for bad in ("cooked", "boiled", "fried", "roasted", "juice", "leaf", "canned"):
            if bad in nm:
                s -= 1
        return s

    return max(cands, key=score)


def _fndi_from_frames(napr: pd.DataFrame, kfct: pd.DataFrame, pop: pd.DataFrame) -> pd.DataFrame:
    if napr.empty or kfct.empty or "food_name" not in kfct.columns:
        return pd.DataFrame()
    napr = napr.copy()
    yr = pd.to_numeric(napr["year"], errors="coerce")
    d = napr[yr == int(yr.max())].copy()
    d["production_mt"] = pd.to_numeric(d["production_mt"], errors="coerce")
    d = d[d["production_mt"].notna() & (d["production_mt"] > 0)]
    if d.empty:
        return pd.DataFrame()

    kf = kfct.copy().reset_index(drop=True)
    foods_norm = {i: _fndi_nrm(n) for i, n in kf["food_name"].items()}
    ef = pd.to_numeric(kf.get("edible_factor"), errors="coerce")
    if ef.notna().any() and ef.median() > 1.5:     # stored as a percentage
        ef = ef / 100.0
    kf["_edible"] = ef.fillna(1.0).clip(lower=0.1, upper=1.0)
    if "vit_a_rae_mcg" in kf.columns and "vit_a_re_mcg" in kf.columns:
        kf["vit_a_rae_mcg"] = pd.to_numeric(kf["vit_a_rae_mcg"], errors="coerce").fillna(
            pd.to_numeric(kf["vit_a_re_mcg"], errors="coerce"))

    # match each crop once -> per-gram nutrient content and edible fraction
    permap = {}
    for c in d["crop"].dropna().unique():
        idx = _match_crop_food(_fndi_nrm(c), foods_norm)
        if idx is None:
            continue
        food = kf.loc[idx]
        per_g = {k: (_fndi_num(food.get(col)) / 100.0) for k, col in _FNDI_NUTRIENTS.items()}
        per_g = {k: (v if pd.notna(v) else 0.0) for k, v in per_g.items()}
        permap[c] = (float(food["_edible"]), per_g)
    if not permap:
        return pd.DataFrame()

    supply, matched = {}, {}
    for _, r in d.iterrows():
        c = r["crop"]
        if c not in permap:
            continue
        edible, per_g = permap[c]
        grams = float(r["production_mt"]) * 1_000_000.0 * edible
        acc = supply.setdefault(r["county_code"], {k: 0.0 for k in _FNDI_NUTRIENTS})
        for k in _FNDI_NUTRIENTS:
            acc[k] += grams * per_g[k]
        matched[r["county_code"]] = matched.get(r["county_code"], 0) + 1

    sup = pd.DataFrame.from_dict(supply, orient="index").reset_index(names="county_code")
    sup["n_crops_matched"] = sup["county_code"].map(matched)
    p = pop[["county_code", "population_total"]].copy()
    p["population_total"] = pd.to_numeric(p["population_total"], errors="coerce")
    sup = sup.merge(p, on="county_code", how="left")
    denom = (sup["population_total"] * 365.0).where(sup["population_total"] > 0)

    out = pd.DataFrame({"county_code": sup["county_code"],
                        "n_crops_matched": sup["n_crops_matched"]})
    for k in _FNDI_NUTRIENTS:
        out[_FNDI_PC_NAME[k]] = sup[k] / denom
    # nutrient density per 1000 kcal of food supply (composition, independent of
    # population): nutrient supply divided by energy supply. This is the "nutrient
    # density per unit of food supply" the framework asks for, distinct from the
    # per-capita quantity above.
    e_supply = sup["energy_kcal"].where(sup["energy_kcal"] > 0)
    for k in _FNDI_NUTRIENTS:
        if k == "energy_kcal":
            continue
        out[_FNDI_PC_NAME[k].replace("_pc_day", "_per_1000kcal")] = sup[k] / e_supply * 1000.0
    comp = [_FNDI_PC_NAME[k] for k in _FNDI_COMPOSITE if _FNDI_PC_NAME[k] in out.columns]
    z = out[comp].apply(lambda s: (s - s.mean()) / s.std(ddof=0) if s.std(ddof=0) else s * 0.0)
    out["food_nutrient_density_index"] = z.mean(axis=1)
    out["napr_year"] = int(yr.max())
    return out


def food_nutrient_density_county(con) -> pd.DataFrame:
    """County per-capita nutrient supply and the composite Food Nutrient Density
    Index from NAPR production and KFCT composition. Empty if inputs are absent."""
    if not (_has_table(con, "food", "napr_crop_county")
            and _has_table(con, "food", "kfct_foods")):
        return pd.DataFrame()
    napr = con.execute("select * from food.napr_crop_county").df()
    kfct = con.execute("select * from food.kfct_foods").df()
    pop = census_population(con)
    if pop.empty or "population_total" not in pop.columns:
        return pd.DataFrame()
    return _fndi_from_frames(napr, kfct, pop)


# --------------------------------------------------------------------------
# derived county indicators: per-capita, land-use intensity, yield
# --------------------------------------------------------------------------
def _add_derived(t: pd.DataFrame) -> pd.DataFrame:
    def have(*names):
        return all(n in t.columns for n in names)
    if have("maize_production_mt", "maize_area_ha"):
        t["maize_yield_t_ha"] = t["maize_production_mt"] / t["maize_area_ha"].where(t["maize_area_ha"] > 0)
    if have("crop_production_mt", "population_total"):
        t["crop_production_per_capita_kg"] = (
            t["crop_production_mt"] * 1000 / t["population_total"].where(t["population_total"] > 0))
    if have("maize_production_mt", "population_total"):
        t["maize_production_per_capita_kg"] = (
            t["maize_production_mt"] * 1000 / t["population_total"].where(t["population_total"] > 0))
    if have("ag_land_ha_total", "land_area_sqkm"):
        t["ag_land_share"] = t["ag_land_ha_total"] / (t["land_area_sqkm"] * 100).where(t["land_area_sqkm"] > 0)
    if have("farming_households_total", "households"):
        t["farming_hh_share"] = t["farming_households_total"] / t["households"].where(t["households"] > 0)
    if have("ag_land_ha_total", "farming_households_total"):
        t["cropland_per_farming_hh_ha"] = (
            t["ag_land_ha_total"] / t["farming_households_total"].where(t["farming_households_total"] > 0))
    if have("ag_land_ha_subsistence", "ag_land_ha_total"):
        t["ag_land_subsistence_share"] = (
            t["ag_land_ha_subsistence"] / t["ag_land_ha_total"].where(t["ag_land_ha_total"] > 0))
    return t


def soil_yield_model(table: pd.DataFrame, yield_col: str = "maize_yield_t_ha"):
    """Exploratory OLS of county maize yield on topsoil quality. Descriptive
    only (soil, management and climate are jointly determined); HC3 errors."""
    try:
        import statsmodels.formula.api as smf
    except Exception:  # noqa: BLE001
        return None
    feats = [p for p in ("phh2o", "soc", "nitrogen", "cec", "clay") if p in table.columns]
    if yield_col not in table.columns or not feats:
        return None
    d = table[[yield_col] + feats].replace([np.inf, -np.inf], np.nan).dropna()
    if len(d) < len(feats) + 5:
        return None
    return smf.ols(f"{yield_col} ~ " + " + ".join(feats), data=d).fit(cov_type="HC3")


def soil_nutrition_model(table: pd.DataFrame, outcome: str = "stunting"):
    """Exploratory OLS of a county child-nutrition outcome (default stunting) on
    topsoil quality, controlling for the staple price and maize yield where
    available. This is the soil-to-nutrition pathway that motivates the bundle.
    It is associational, not causal: soil, climate, market access, diets and
    care practices are jointly determined, so it is reported with HC3 errors and
    read as a conditional gradient, not an effect."""
    try:
        import statsmodels.formula.api as smf
    except Exception:  # noqa: BLE001
        return None
    if outcome not in table.columns:
        return None
    soil = [p for p in ("phh2o", "soc", "nitrogen", "cec") if p in table.columns]
    controls = [c for c in ("maize_price_median", "maize_yield_t_ha") if c in table.columns]
    feats = soil + controls
    if not soil:
        return None
    d = table[[outcome] + feats].replace([np.inf, -np.inf], np.nan).dropna()
    if len(d) < len(feats) + 5:
        return None
    return smf.ols(f"{outcome} ~ " + " + ".join(feats), data=d).fit(cov_type="HC3")


def _ols_report(table: pd.DataFrame, outcome: str, regressors, min_extra: int = 5):
    """Fit OLS of outcome on whichever regressors are present, with HC3 robust
    standard errors. Returns (fitted_model, used_regressors) or (None, [])."""
    try:
        import statsmodels.formula.api as smf
    except Exception:  # noqa: BLE001
        return None, []
    if outcome not in table.columns:
        return None, []
    used = [r for r in regressors if r in table.columns]
    if not used:
        return None, []
    d = table[[outcome] + used].replace([np.inf, -np.inf], np.nan).dropna()
    if len(d) < len(used) + min_extra:
        return None, []
    model = smf.ols(f"{outcome} ~ " + " + ".join(used), data=d).fit(cov_type="HC3")
    return model, used


def stage1_food_density_model(table: pd.DataFrame):
    """Stage 1 of the pathway: county food nutrient density on the soil index and
    land / climate controls. The soil index now carries the iSDA micronutrients,
    so this is the first specification in which the soil-to-food link uses P, K,
    Zn and Fe. Associational (HC3 errors); identified between counties because
    soil is time-invariant. Returns (model, used_regressors)."""
    return _ols_report(
        table, "food_nutrient_density_index",
        ["soil_index", "ag_land_share", "farming_hh_share",
         "cropland_per_farming_hh_ha", "crop_diversity_shannon",
         "rain_mm_mean", "drought_freq"])


def stage2_nutrition_model(table: pd.DataFrame, outcome: str = "stunting"):
    """Stage 2 of the pathway: child nutrition on food nutrient density and the
    soil index, controlling for wealth, maternal education and water and
    sanitation. This replaces the price-limited model (which ran on 14 counties)
    and estimates at full county coverage, since the food density index and the
    KDHS controls exist for all 47 counties. Associational, HC3 errors; read as a
    conditional gradient, not an effect. Returns (model, used_regressors)."""
    return _ols_report(
        table, outcome,
        ["food_nutrient_density_index", "soil_index", "wealth_factor_mean",
         "edu_years_mean", "improved_water_share", "improved_sanitation_share",
         "diarrhea_share", "rain_mm_mean"])


def stage4_policy_response_model(table: pd.DataFrame):
    """Stage 4: does county policy intensity respond to nutrient need, or to
    agricultural potential? Regress the Policy Signal Index on the body, food and
    soil nutrient gaps, controlling for maize production potential and population
    density. Because the fertilizer rollout was maize-belt-first, a signal that
    loads on potential rather than on the need gaps is itself the policy finding:
    effort tracked where production could rise, not where nutrient gaps were
    widest. Associational, HC3 errors. Returns (model, used_regressors)."""
    return _ols_report(
        table, "policy_signal_index",
        ["gap_body_stunting", "gap_food_density", "gap_soil",
         "maize_production_per_capita_kg", "maize_yield_t_ha", "density"])


def stage5_persistence_model(table: pd.DataFrame):
    """Stage 5 substitute, the closest dynamic the data allows: 2022 stunting on
    its 2014 level (the lagged outcome) plus the soil index, food nutrient density
    and body-vector controls. The coefficient on stunting_2014 is the persistence
    term; the soil and food coefficients are the conditional gradient net of the
    2014 baseline. A full panel VAR is not feasible (soil is time-invariant and
    only two body time points exist), so this lagged-outcome model is the honest
    stand-in. Associational, HC3 errors. Returns (model, used_regressors)."""
    return _ols_report(
        table, "stunting",
        ["stunting_2014", "food_nutrient_density_index", "soil_index",
         "wealth_factor_mean", "edu_years_mean", "improved_water_share"])


def kdhs_vs_actionplan(table: pd.DataFrame) -> pd.DataFrame:
    """Validate the survey aggregates against the four county stunting figures the
    Action Plan names (Kilifi, West Pokot, Samburu, Kisumu): KDHS estimate, Plan
    figure and their difference."""
    if "stunting" not in table.columns or "stunting_actionplan" not in table.columns:
        return pd.DataFrame()
    d = table.loc[table["stunting_actionplan"].notna(),
                  ["county_name", "stunting", "stunting_actionplan"]].copy()
    if d.empty:
        return d
    d["difference"] = (d["stunting"] - d["stunting_actionplan"]).round(1)
    return d.sort_values("stunting_actionplan", ascending=False).reset_index(drop=True)


def descriptive_stats(table: pd.DataFrame) -> pd.DataFrame:
    num = table.select_dtypes(include="number")
    desc = num.describe(percentiles=[0.25, 0.5, 0.75]).T
    desc["missing"] = table.shape[0] - num.count()
    return desc.round(3)


# --------------------------------------------------------------------------
# soil-health typology: standardise, k-means, choose k by silhouette
# --------------------------------------------------------------------------
def soil_typology(table: pd.DataFrame, k_range=range(3, 8), seed: int = 42):
    from sklearn.cluster import KMeans
    from sklearn.metrics import silhouette_score
    from sklearn.preprocessing import StandardScaler

    feats = [p for p in list(SOILGRIDS) + ["p_isda", "k_isda", "zn_isda", "fe_isda"]
             if p in table.columns]
    X = table[feats].dropna()
    if len(X) < max(k_range) + 1:
        return table.assign(soil_zone=np.nan), None, feats
    Xs = StandardScaler().fit_transform(X)
    best_k, best_s, best_labels = None, -1, None
    for k in k_range:
        km = KMeans(n_clusters=k, n_init=10, random_state=seed).fit(Xs)
        s = silhouette_score(Xs, km.labels_)
        if s > best_s:
            best_k, best_s, best_labels = k, s, km.labels_
    table = table.copy()
    table.loc[X.index, "soil_zone"] = best_labels
    return table, {"k": best_k, "silhouette": round(best_s, 3)}, feats


# --------------------------------------------------------------------------
# geometry for choropleth maps
# --------------------------------------------------------------------------
def county_geometry(base: Path):
    import geopandas as gpd
    from .crosswalk import find_admin_layers, norm

    layers = find_admin_layers(base / "data" / "raw")
    adm = layers.get("adm1") or layers.get("adm2")
    if not adm:
        return None
    g = (gpd.read_file(adm["path"], layer=adm["layer"]) if adm["layer"]
         else gpd.read_file(adm["path"])).to_crs("EPSG:4326")
    name_col = adm["adm1_name"] or next(
        (c for c in g.columns if "name" in c.lower() or "county" in c.lower()), None)
    g = g[[name_col, "geometry"]].rename(columns={name_col: "county_name"})
    g["county_norm"] = g["county_name"].map(norm)
    if adm["count"] > 60:  # adm2 fallback: dissolve to county
        g = g.dissolve(by="county_norm", as_index=False)
    return g


def choropleth(gdf, table: pd.DataFrame, column: str, title: str, out: Path,
               cmap: str = "YlOrBr"):
    import matplotlib.pyplot as plt

    if column not in table.columns:
        return None
    merged = gdf.merge(table[["county_norm", column]], on="county_norm", how="left")
    if merged[column].notna().sum() == 0:
        print(f"[analysis] map skipped: {column} has no values to plot")
        return None
    fig, ax = plt.subplots(1, 1, figsize=(7, 7))
    merged.plot(column=column, ax=ax, cmap=cmap, legend=True, edgecolor="white",
                linewidth=0.3, missing_kwds={"color": "lightgrey", "label": "no data"})
    ax.set_title(title, fontsize=12)
    ax.axis("off")
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out


# --------------------------------------------------------------------------
# exploratory soil-price association (associational, not causal)
# --------------------------------------------------------------------------
def soil_price_model(table: pd.DataFrame, price_col: str = "maize_price_median"):
    """OLS of county staple price on topsoil quality. This is descriptive: soil
    quality, market access and prices are jointly determined, so coefficients
    are associations, not causal effects. Reported with robust (HC3) errors."""
    try:
        import statsmodels.formula.api as smf
    except Exception:  # noqa: BLE001
        return None
    feats = [p for p in ("phh2o", "soc", "nitrogen", "cec", "clay") if p in table.columns]
    if price_col not in table.columns or not feats:
        return None
    d = table[[price_col] + feats].dropna()
    if len(d) < len(feats) + 5:
        return None
    formula = f"{price_col} ~ " + " + ".join(feats)
    return smf.ols(formula, data=d).fit(cov_type="HC3")


# --------------------------------------------------------------------------
# policy figures and the soil <-> Action-Plan-nutrition cross-check
# --------------------------------------------------------------------------
def barh(df: pd.DataFrame, label_col: str, value_col: str, title: str, out: Path,
         xlabel: str = "", color: str = "#2E75B6"):
    """Horizontal bar chart (used for the budget-by-transition figure)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = df.sort_values(value_col)
    fig, ax = plt.subplots(1, 1, figsize=(8, 0.6 * len(d) + 1.5))
    ax.barh(d[label_col].astype(str), d[value_col], color=color)
    for y, v in enumerate(d[value_col]):
        ax.text(v, y, f" {v:,.0f}", va="center", fontsize=9)
    ax.set_title(title, fontsize=12)
    ax.set_xlabel(xlabel)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out


def policy_nutrition_cross_check(table: pd.DataFrame) -> pd.DataFrame:
    """Place the four counties the Action Plan names alongside their soil and
    price profile from the database. With n=4 this is an illustrative external
    check on the soil and price layers, not an inferential analysis."""
    if "stunting_actionplan" not in table.columns:
        return pd.DataFrame()
    cols = [c for c in ("county_name", "stunting_actionplan", "soc", "phh2o",
                        "nitrogen", "cec", "maize_price_median", "maize_price_cv")
            if c in table.columns]
    out = table.loc[table["stunting_actionplan"].notna(), cols].copy()
    return out.sort_values("stunting_actionplan", ascending=False).reset_index(drop=True)
