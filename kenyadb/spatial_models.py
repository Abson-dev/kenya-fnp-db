"""Refined soil specification and spatial diagnostics.

Two analyses the robustness battery and the stage models call for.

1. Soil refinement. The seven-property composite is null in every stage, yet
   extractable iron (fe_isda) alone predicts both food density and dietary
   diversity. These functions re-estimate Stage 1, the diet model and Stage 2
   with iron and with a PCA sparse index in place of the composite.

2. Spatial diagnostics. The soil index has a Moran's I of about 0.64, so the
   non-spatial errors understate uncertainty. These functions test the Stage 1
   and Stage 2 residuals for residual clustering and fit a spatially-lagged-X
   (SLX) Stage 1 that adds the neighbours' soil and farming structure.

All associational, HC3 robust errors, read as between-county gradients. Built on
analysis.build_county_table.

Author: Aboubacar HEMA
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import analysis as A
from .robustness import _S1_CONTROLS, _morans_i


# ===================== soil refinement =====================

def _add_pca_soil(table: pd.DataFrame) -> pd.DataFrame:
    """Add soil_pca1, the first principal component of the standardized soil
    properties, oriented so that higher means more fertile."""
    present = [c for c in A.SOIL_INDEX_POS if c in table.columns]
    if len(present) < 3:
        return table
    try:
        from sklearn.decomposition import PCA
        from sklearn.preprocessing import StandardScaler
    except Exception:  # noqa: BLE001
        return table
    d = table[present].replace([np.inf, -np.inf], np.nan).dropna()
    if len(d) < 10:
        return table
    X = StandardScaler().fit_transform(d.values)
    pca = PCA(n_components=1).fit(X)
    comp = pca.transform(X)[:, 0]
    if pca.components_[0].mean() < 0:   # orient: higher = more fertile
        comp = -comp
    t = table.copy()
    t.loc[d.index, "soil_pca1"] = comp
    return t


def refined_soil_models(table: pd.DataFrame) -> pd.DataFrame:
    """Stage 1, the diet model and Stage 2 re-estimated with iron and a PCA sparse
    index against the composite baseline. One row per specification."""
    rows = []

    def grab(label, outcome, soil_term, model):
        if model is None:
            return
        rows.append({
            "model": label, "outcome": outcome, "soil_term": soil_term,
            "n": int(model.nobs), "r2": round(model.rsquared, 3),
            "b_soil": round(model.params.get(soil_term, np.nan), 4),
            "p_soil": round(model.pvalues.get(soil_term, np.nan), 3)})

    grab("Stage 1 composite", "food density", "soil_index",
         A.stage1_food_density_model(table)[0])
    grab("Stage 1 iron", "food density", "fe_isda",
         A._ols_report(table, "food_nutrient_density_index", ["fe_isda"] + _S1_CONTROLS)[0])
    t = _add_pca_soil(table)
    if "soil_pca1" in t.columns:
        grab("Stage 1 PCA1", "food density", "soil_pca1",
             A._ols_report(t, "food_nutrient_density_index", ["soil_pca1"] + _S1_CONTROLS)[0])
    if "mdd" in table.columns:
        diet = ["wealth_factor_mean", "edu_years_mean", "improved_water_share"]
        grab("Diet composite", "mdd", "soil_index",
             A._ols_report(table, "mdd", ["soil_index"] + diet)[0])
        grab("Diet iron", "mdd", "fe_isda",
             A._ols_report(table, "mdd", ["fe_isda"] + diet)[0])
    grab("Stage 2 iron", "stunting", "fe_isda",
         A._ols_report(table, "stunting",
                       ["food_nutrient_density_index", "fe_isda", "wealth_factor_mean",
                        "edu_years_mean", "improved_water_share",
                        "improved_sanitation_share", "diarrhea_share", "rain_mm_mean"])[0])
    return pd.DataFrame(rows)


# ===================== spatial diagnostics =====================

def _county_weights(base: Path, table: pd.DataFrame):
    """Row-standardized queen-contiguity weights aligned to the table's counties.
    Returns (W, order) where order is the list of county_norm in W row order, or
    (None, None) if geometry is unavailable."""
    try:
        import geopandas  # noqa: F401
    except Exception:  # noqa: BLE001
        return None, None
    if "county_norm" not in table.columns:
        return None, None
    g = A.county_geometry(base)
    if g is None or "county_norm" not in g.columns:
        return None, None
    if g["county_norm"].duplicated().any():
        g = g.dissolve(by="county_norm", as_index=False)
    g = g[g["county_norm"].isin(set(table["county_norm"]))].reset_index(drop=True)
    if len(g) < 10:
        return None, None
    geoms = g.geometry.buffer(0.01)
    n = len(g)
    W = np.zeros((n, n))
    for i in range(n):
        hits = geoms.intersects(geoms.iloc[i]).to_numpy(copy=True)
        hits[i] = False
        W[i, hits] = 1.0
    rs = W.sum(axis=1, keepdims=True)
    rs[rs == 0] = 1
    return W / rs, list(g["county_norm"])


def residual_spatial(table: pd.DataFrame, base: Path) -> pd.DataFrame:
    """Moran's I on the Stage 1 and Stage 2 residuals. Significant residual
    clustering means the non-spatial model leaves spatial structure in the errors,
    so its standard errors are too small."""
    W, order = _county_weights(base, table)
    if W is None:
        return pd.DataFrame()
    rows = []
    fits = [("Stage 1", A.stage1_food_density_model(table)),
            ("Stage 2 stunting", A.stage2_nutrition_model(table))]
    for label, (model, _used) in fits:
        if model is None:
            continue
        res = pd.Series(np.asarray(model.resid),
                        index=table.loc[model.resid.index, "county_norm"].values)
        vals = np.array([res.get(cn, np.nan) for cn in order], dtype=float)
        ok = ~np.isnan(vals)
        if ok.sum() < len(order) - 5:
            continue
        if ok.all():
            i_val, ei, p = _morans_i(vals, W)
        else:
            idx = np.where(ok)[0]
            Wi = W[np.ix_(idx, idx)]
            r = Wi.sum(axis=1, keepdims=True)
            r[r == 0] = 1
            i_val, ei, p = _morans_i(vals[idx], Wi / r)
        rows.append({"model": label, "resid_morans_i": round(i_val, 3),
                     "expected_i": round(ei, 3), "perm_p": round(p, 3)})
    return pd.DataFrame(rows)


def slx_stage1(table: pd.DataFrame, base: Path):
    """Spatially-lagged-X Stage 1: add the neighbours' soil index and farming
    share (W x soil_index, W x farming_hh_share) to the food-density model. Returns
    (coef_frame, info). A meaningful neighbour term with the residual Moran's I
    falling toward zero is the spatial reading."""
    W, order = _county_weights(base, table)
    if W is None:
        return pd.DataFrame(), {}
    t = table[table["county_norm"].isin(order)].copy()
    t = t.set_index("county_norm").loc[order].reset_index()
    lagcols = []
    for col in ("soil_index", "farming_hh_share"):
        if col in t.columns:
            x = t[col].to_numpy(dtype=float)
            xf = np.where(np.isnan(x), np.nanmean(x), x)
            t[f"w_{col}"] = W @ xf
            lagcols.append(f"w_{col}")
    model, _used = A._ols_report(
        t, "food_nutrient_density_index", ["soil_index"] + _S1_CONTROLS + lagcols)
    if model is None:
        return pd.DataFrame(), {}
    coef = pd.DataFrame([
        {"term": k, "coef": round(model.params[k], 4), "p": round(model.pvalues[k], 3)}
        for k in model.params.index if k != "Intercept"])
    # residual Moran's I of the SLX model
    res = pd.Series(np.asarray(model.resid),
                    index=t.loc[model.resid.index, "county_norm"].values)
    vals = np.array([res.get(cn, np.nan) for cn in order], dtype=float)
    ok = ~np.isnan(vals)
    if ok.all():
        i_val, _ei, p = _morans_i(vals, W)
    elif ok.sum() >= len(order) - 5:
        idx = np.where(ok)[0]
        Wi = W[np.ix_(idx, idx)]
        r = Wi.sum(axis=1, keepdims=True)
        r[r == 0] = 1
        i_val, _ei, p = _morans_i(vals[idx], Wi / r)
    else:
        i_val, p = np.nan, np.nan
    info = {"n": int(model.nobs), "r2": round(model.rsquared, 3),
            "resid_morans_i": round(i_val, 3) if i_val == i_val else None,
            "resid_perm_p": round(p, 3) if p == p else None}
    return coef, info


def _write_summary(refined, resid, slx_coef, slx_info, path: Path) -> None:
    lines = ["# Refined soil specification and spatial diagnostics", "",
             "Associational, HC3 robust errors, between-county. Soil refinement",
             "addresses the null composite; the spatial section addresses the",
             "soil Moran's I of about 0.64.", "",
             "## Soil refinement: iron and a PCA index vs the composite", ""]
    lines.append(refined.to_markdown(index=False) if not refined.empty else "Not available.")
    lines += ["", "## Residual spatial autocorrelation", ""]
    lines.append(resid.to_markdown(index=False) if not resid.empty
                 else "Not available (geopandas or boundaries absent).")
    lines += ["", "## Spatially-lagged-X Stage 1", ""]
    if not slx_coef.empty:
        lines.append(slx_coef.to_markdown(index=False))
        lines += ["", f"n = {slx_info.get('n')}, R-squared = {slx_info.get('r2')}, "
                  f"residual Moran's I = {slx_info.get('resid_morans_i')} "
                  f"(p = {slx_info.get('resid_perm_p')})."]
    else:
        lines.append("Not available (geopandas or boundaries absent).")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def run(con, base: Path, out: Path) -> dict:
    """Run the refinement and spatial diagnostics on the built table and write the
    outputs to `out`. Returns a dict of result frames."""
    out = Path(out)
    table = A.build_county_table(con)
    refined = refined_soil_models(table)
    resid = residual_spatial(table, base)
    slx_coef, slx_info = slx_stage1(table, base)
    for name, df in (("refined_soil_models", refined),
                     ("spatial_residuals", resid),
                     ("slx_stage1", slx_coef)):
        if isinstance(df, pd.DataFrame) and not df.empty:
            df.to_csv(out / f"{name}.csv", index=False)
    _write_summary(refined, resid, slx_coef, slx_info,
                   out / "refinement_spatial_summary.md")
    return {"refined_soil_models": refined, "spatial_residuals": resid,
            "slx_stage1": slx_coef, "slx_info": slx_info}
