"""Robustness and sensitivity battery (prompt Section 6).

Executes the checks the empirical strategy specifies, as far as the Kenya
cross-section allows: alternative body outcomes, individual soil indicators,
alternative soil-index constructions, the no-soil counterfactual for the food
equation, subsample analysis, and a spatial-autocorrelation test. Everything is
associational with HC3 robust errors and is read as a between-county gradient,
not a causal effect. Built on the analytical table from
analysis.build_county_table.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import analysis as A

_S1_CONTROLS = ["ag_land_share", "farming_hh_share", "cropland_per_farming_hh_ha",
                "crop_diversity_shannon", "rain_mm_mean", "drought_freq"]


def _zscore(s: pd.Series) -> pd.Series:
    sd = s.std(ddof=0)
    return (s - s.mean()) / sd if sd else s * 0.0


def alternative_outcomes(table: pd.DataFrame) -> pd.DataFrame:
    """Stage 2 re-estimated for each available body outcome (prompt 6: alternative
    body indicators). Anaemia is unavailable in both KDHS rounds, so it cannot
    enter. One row per outcome with the food-density and soil coefficients."""
    rows = []
    targets = [("stunting", "child stunting"), ("wasting", "child wasting"),
               ("underweight", "child underweight"), ("mdd", "dietary diversity (MDD)")]
    for col, label in targets:
        if col not in table.columns or table[col].notna().sum() < 25:
            continue
        m, _ = A.stage2_nutrition_model(table, outcome=col)
        if m is None:
            continue
        rows.append({
            "outcome": label, "n": int(m.nobs), "r2": round(m.rsquared, 3),
            "b_food_density": round(m.params.get("food_nutrient_density_index", np.nan), 4),
            "p_food_density": round(m.pvalues.get("food_nutrient_density_index", np.nan), 3),
            "b_soil_index": round(m.params.get("soil_index", np.nan), 4),
            "p_soil_index": round(m.pvalues.get("soil_index", np.nan), 3)})
    return pd.DataFrame(rows)


def individual_soil_indicators(table: pd.DataFrame) -> pd.DataFrame:
    """Stage 1 food-density regressed on each raw soil property on its own (prompt
    6: individual indicators instead of the composite), with the same land and
    climate controls. Shows which properties carry the soil-to-food gradient that
    the composite bundles."""
    rows = []
    for prop in A.SOIL_INDEX_POS + ["soil_index"]:
        if prop not in table.columns:
            continue
        m, _ = A._ols_report(table, "food_nutrient_density_index", [prop] + _S1_CONTROLS)
        if m is None:
            continue
        rows.append({"soil_term": prop, "n": int(m.nobs), "r2": round(m.rsquared, 3),
                     "b": round(m.params.get(prop, np.nan), 4),
                     "p": round(m.pvalues.get(prop, np.nan), 3)})
    return pd.DataFrame(rows)


def soil_index_variants(table: pd.DataFrame):
    """Build the soil index three ways (z-score, min-max, rank) from the same
    properties and (a) report their rank correlations and (b) re-estimate Stage 1
    under each (prompt 6: alternative index-construction methods). A stable soil
    sign across constructions is the robustness signal. Returns (corr, stage1)."""
    present = [c for c in A.SOIL_INDEX_POS if c in table.columns]
    if len(present) < 3:
        return pd.DataFrame(), pd.DataFrame()
    t = table.copy()
    X = t[present]
    t["soil_index_z"] = X.apply(_zscore).mean(axis=1)
    t["soil_index_minmax"] = X.apply(
        lambda s: (s - s.min()) / (s.max() - s.min()) if s.max() > s.min() else s * 0.0
    ).mean(axis=1)
    t["soil_index_rank"] = X.rank().apply(
        lambda s: (s - 1) / (s.notna().sum() - 1) if s.notna().sum() > 1 else s * 0.0
    ).mean(axis=1)
    variants = ["soil_index_z", "soil_index_minmax", "soil_index_rank"]
    corr = t[variants].corr(method="spearman").round(3)
    corr.insert(0, "construction", [v.replace("soil_index_", "") for v in variants])
    rows = []
    for variant in variants:
        m, _ = A._ols_report(t, "food_nutrient_density_index", [variant] + _S1_CONTROLS)
        if m is None:
            continue
        rows.append({"construction": variant.replace("soil_index_", ""),
                     "n": int(m.nobs), "r2": round(m.rsquared, 3),
                     "b_soil": round(m.params.get(variant, np.nan), 4),
                     "p_soil": round(m.pvalues.get(variant, np.nan), 3)})
    return corr.reset_index(drop=True), pd.DataFrame(rows)


def no_soil_counterfactual(table: pd.DataFrame) -> pd.DataFrame:
    """Stage 1 with and without the soil index (prompt 6: no-soil-pathway
    counterfactual). The drop in R-squared is the share of between-county food-
    density variation the soil pathway accounts for, over land and climate."""
    full, _ = A.stage1_food_density_model(table)
    nosoil, _ = A._ols_report(table, "food_nutrient_density_index", _S1_CONTROLS)
    if full is None or nosoil is None:
        return pd.DataFrame()
    return pd.DataFrame([
        {"model": "with soil index", "n": int(full.nobs),
         "r2": round(full.rsquared, 3), "adj_r2": round(full.rsquared_adj, 3)},
        {"model": "no soil (land + climate only)", "n": int(nosoil.nobs),
         "r2": round(nosoil.rsquared, 3), "adj_r2": round(nosoil.rsquared_adj, 3)},
        {"model": "soil contribution (delta)", "n": int(full.nobs),
         "r2": round(full.rsquared - nosoil.rsquared, 3),
         "adj_r2": round(full.rsquared_adj - nosoil.rsquared_adj, 3)},
    ])


def _subsample_model(d: pd.DataFrame):
    """Parsimonious Stage 2 for a small subsample: stunting on food density, soil
    and wealth, kept short to preserve degrees of freedom at n about 23."""
    return A._ols_report(
        d, "stunting",
        ["food_nutrient_density_index", "soil_index", "wealth_factor_mean"],
        min_extra=3)


def subsample_analysis(table: pd.DataFrame) -> pd.DataFrame:
    """Re-estimate the food-to-body gradient in policy-relevant subsamples (prompt
    6): arid / semi-arid versus high-potential, rural versus urban, and high
    versus low poverty. Splits at the median of an available proxy. Small samples,
    so read as indicative rather than precise."""
    rows = []
    splits = []
    if "rain_mm_mean" in table.columns:
        splits.append(("agroecology", "rain_mm_mean", "ASAL (drier)", "high-potential (wetter)"))
    elif "drought_freq" in table.columns:
        splits.append(("agroecology", "drought_freq", "high-potential", "ASAL (more drought)"))
    if "urban_share" in table.columns:
        splits.append(("settlement", "urban_share", "more rural", "more urban"))
    if "wealth_factor_mean" in table.columns:
        splits.append(("poverty", "wealth_factor_mean", "poorer", "less poor"))
    for name, var, lo_label, hi_label in splits:
        d = table[table[var].notna()].copy()
        if len(d) < 20:
            continue
        med = d[var].median()
        for label, sub in ((lo_label, d[d[var] <= med]), (hi_label, d[d[var] > med])):
            m, _ = _subsample_model(sub)
            if m is None:
                continue
            rows.append({
                "split": name, "subsample": label, "n": int(m.nobs),
                "r2": round(m.rsquared, 3),
                "b_food_density": round(m.params.get("food_nutrient_density_index", np.nan), 4),
                "p_food_density": round(m.pvalues.get("food_nutrient_density_index", np.nan), 3),
                "b_soil_index": round(m.params.get("soil_index", np.nan), 4)})
    return pd.DataFrame(rows)


def _morans_i(values: np.ndarray, W: np.ndarray):
    """Moran's I for a value vector and a row-standardized weights matrix W.
    Returns (I, expected_I, permutation_p) with a two-sided permutation test."""
    x = np.asarray(values, float)
    n = len(x)
    z = x - x.mean()
    s0 = W.sum()
    den = (z ** 2).sum()
    if den == 0 or s0 == 0:
        return np.nan, np.nan, np.nan
    obs = (n / s0) * (z @ (W @ z)) / den
    ei = -1.0 / (n - 1)
    rng = np.random.default_rng(42)
    perm = np.empty(999)
    for k in range(999):
        zp = rng.permutation(z)
        perm[k] = (n / s0) * (zp @ (W @ zp)) / den
    p = (1 + np.sum(np.abs(perm - ei) >= np.abs(obs - ei))) / 1000.0
    return obs, ei, p


def spatial_autocorrelation(table: pd.DataFrame, base: Path) -> pd.DataFrame:
    """Moran's I for the key county variables using queen contiguity from the
    county boundaries (prompt 6: spatial spillovers). A positive, significant I
    means neighbouring counties resemble each other more than chance, so the
    pathway has spatial structure that a non-spatial error term understates."""
    try:
        import geopandas  # noqa: F401
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    g = A.county_geometry(base)
    if g is None or "county_norm" not in g.columns:
        return pd.DataFrame()
    if g["county_norm"].duplicated().any():
        g = g.dissolve(by="county_norm", as_index=False)
    g = g.reset_index(drop=True)
    geoms = g.geometry.buffer(0.01)  # small buffer closes sliver gaps between polygons
    n = len(g)
    W = np.zeros((n, n))
    for i in range(n):
        hits = geoms.intersects(geoms.iloc[i]).to_numpy(copy=True)
        hits[i] = False
        W[i, hits] = 1.0
    rs = W.sum(axis=1, keepdims=True)
    rs[rs == 0] = 1
    W = W / rs
    rows = []
    targets = [("stunting", "child stunting"),
               ("food_nutrient_density_index", "food nutrient density"),
               ("soil_index", "soil index"),
               ("stunting_change", "stunting change 2014 to 2022")]
    for col, label in targets:
        if col not in table.columns:
            continue
        m = g.merge(table[["county_norm", col]], on="county_norm", how="left")
        vals = m[col].to_numpy(dtype=float)
        ok = ~np.isnan(vals)
        if ok.sum() < n - 5:
            continue
        if ok.all():
            i_val, ei, p = _morans_i(vals, W)
        else:
            idx = np.where(ok)[0]
            Wi = W[np.ix_(idx, idx)]
            r2 = Wi.sum(axis=1, keepdims=True)
            r2[r2 == 0] = 1
            i_val, ei, p = _morans_i(vals[idx], Wi / r2)
        rows.append({"variable": label, "morans_i": round(i_val, 3),
                     "expected_i": round(ei, 3), "perm_p": round(p, 3)})
    return pd.DataFrame(rows)


def _write_summary(results: dict, path: Path) -> None:
    lines = ["# Robustness and sensitivity battery", "",
             "Prompt Section 6, executed on the county analytical table. All models",
             "are associational with HC3 robust standard errors and are read as",
             "between-county gradients, not causal effects.", ""]
    titles = {
        "alternative_outcomes": "Alternative body outcomes (Stage 2)",
        "individual_soil_indicators": "Individual soil indicators (Stage 1)",
        "soil_index_variant_corr": "Soil-index constructions: rank correlation",
        "soil_index_variant_stage1": "Soil-index constructions: Stage 1 soil coefficient",
        "no_soil_counterfactual": "No-soil counterfactual (Stage 1)",
        "subsample_analysis": "Subsample analysis (Stage 2 stunting)",
        "spatial_autocorrelation": "Spatial autocorrelation (Moran's I)",
    }
    for key, title in titles.items():
        df = results.get(key)
        lines.append(f"## {title}")
        lines.append("")
        if isinstance(df, pd.DataFrame) and not df.empty:
            lines.append(df.to_markdown(index=False))
        else:
            lines.append("Not available (inputs absent or geopandas not installed).")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def run(con, base: Path, out: Path) -> dict:
    """Run the full battery on the built analytical table and write the outputs to
    `out` (the analysis output directory). Returns a dict of result frames."""
    out = Path(out)
    table = A.build_county_table(con)
    results = {
        "alternative_outcomes": alternative_outcomes(table),
        "individual_soil_indicators": individual_soil_indicators(table),
        "no_soil_counterfactual": no_soil_counterfactual(table),
        "subsample_analysis": subsample_analysis(table),
        "spatial_autocorrelation": spatial_autocorrelation(table, base),
    }
    corr, s1 = soil_index_variants(table)
    results["soil_index_variant_corr"] = corr
    results["soil_index_variant_stage1"] = s1
    for name, df in results.items():
        if isinstance(df, pd.DataFrame) and not df.empty:
            df.to_csv(out / f"robustness_{name}.csv", index=False)
    _write_summary(results, out / "robustness_summary.md")
    return results
