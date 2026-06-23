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
def build_county_table(con) -> pd.DataFrame:
    counties = con.execute("""
        select distinct county_code, county_name, county_norm
        from core.crosswalk_admin where county_norm <> ''
    """).df()
    soil = soil_topsoil(con)
    table = counties.merge(soil.drop(columns=["county_name"], errors="ignore"),
                           on="county_norm", how="left")
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
    table = _add_derived(table)
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

    feats = [p for p in SOILGRIDS if p in table.columns]
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
