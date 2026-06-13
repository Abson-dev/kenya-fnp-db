"""Analysis layer for the Kenya soil / food / nutrition / policy database.

Reads the assembled DuckDB database and produces a county-level analytical
dataset plus the first set of publication-oriented outputs:

  1. county analytical table (one row per county): topsoil (0-30 cm) soil
     properties in conventional units, and WFP staple-price level + volatility
  2. descriptive statistics (Table 1)
  3. soil-health typology (k-means county clustering, k chosen by silhouette)
  4. price geography (county median price and volatility, mapped)
  5. an exploratory, explicitly associational soil-price model (scaffold)

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
    # KDHS county nutrition outcomes (stunting, wasting, anaemia) join here once
    # the survey is ingested:  table = table.merge(kdhs_county, on="county_norm")
    return table.sort_values("county_code").reset_index(drop=True)


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

    merged = gdf.merge(table[["county_norm", column]], on="county_norm", how="left")
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
