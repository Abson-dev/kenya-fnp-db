"""Child-level multilevel model of nutrition on local soil and environment.

This is the specification the county aggregates could not identify: each child's
height-for-age is linked to the soil, rainfall and vegetation sampled at the
child's own DHS cluster (not a county mean), with household socioeconomic
controls and a county random intercept. It is the proper test of the soil-to-body
limb, because it separates the local nutrient environment from the county-level
socioeconomic structure that confounded Stage 2.

Inputs:
  - the DHS children's recode (KR) under data/external/<subdir>/, for HAZ and the
    child and household controls (read with the same conventions as kdhs_county);
  - the cluster covariate table data/processed/health/<cluster_csv>.csv built by
    kenyadb.dhs_gps, joined to the child on the cluster id (v001 = dhsclust).

Outputs (to the analysis output directory):
  - multilevel_haz_<round>.txt        the fitted MixedLM summary
  - multilevel_coefficients.csv       tidy fixed-effects table across rounds
  - multilevel_summary.md             a short readable note

Requires pyreadstat (for the .dta recode) and statsmodels (for MixedLM); skips
cleanly when either, or the cluster table, is absent.

Author: Aboubacar HEMA
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from . import transforms as T
from .crosswalk import norm

# Cluster environment covariates (standardized) entered alongside the soil term.
_ENV_COVARS = ["rain_mm_mean", "ndvi_mean"]
# Child and household controls (natural units).
_CHILD_CONTROLS = ["child_age_months", "child_female", "mother_edu_years", "wealth"]
_SOIL_STEMS = ["soc", "nitrogen", "cec", "p_isda", "k_isda", "zn_isda", "fe_isda"]


def _zscore(s: pd.Series) -> pd.Series:
    sd = s.std(ddof=0)
    return (s - s.mean()) / sd if sd else s * 0.0


def build_child_table(base: Path, subdir: str, cluster_csv: str,
                      round_label: str) -> pd.DataFrame | None:
    """Read the KR recode, compute child HAZ and controls, and merge the cluster
    covariates on v001 = dhsclust. Returns a child-level frame or None."""
    try:
        import pyreadstat  # noqa: F401
    except Exception:  # noqa: BLE001
        print("[multilevel] pyreadstat not installed; cannot read the .dta recode")
        return None
    import pyreadstat

    cluster_path = base / "data" / "processed" / "health" / f"{cluster_csv}.csv"
    if not cluster_path.exists():
        print(f"[multilevel] {round_label}: cluster table {cluster_csv}.csv not built yet "
              "(run run_all.py --layer health); skipped")
        return None
    clusters = pd.read_csv(cluster_path)
    if "dhsclust" not in clusters.columns:
        print(f"[multilevel] {round_label}: cluster table has no dhsclust column; skipped")
        return None

    xwalk_path = base / "data" / "processed" / "crosswalk_admin.csv"
    county_norms = (set(pd.read_csv(xwalk_path, dtype=str)["county_norm"]) - {""}
                    if xwalk_path.exists() else set())

    files = T._kdhs_files(base, subdir)
    need = [T._KDHS_VARS["haz"], "v001"]
    extra = ["v005", "hw1", "b4", "v133", "v106", "v190"]
    kr = kr_name = ccol = None
    for f in files:
        try:
            _, meta = pyreadstat.read_dta(str(f), metadataonly=True)
        except Exception:  # noqa: BLE001
            continue
        cols = set(meta.column_names)
        if not all(v in cols for v in need) or "kr" not in str(f).lower():
            continue
        ccol = T._pick_county_col(meta, cols, county_norms) if county_norms else None
        usecols = list(dict.fromkeys([c for c in (need + extra + ([ccol] if ccol else []))
                                      if c in cols]))
        kr, meta = pyreadstat.read_dta(str(f), usecols=usecols)
        if ccol:
            labels = (meta.variable_value_labels or {}).get(ccol, {})
            kr["county_norm"] = kr[ccol].map(labels).map(
                lambda x: norm(x) if isinstance(x, str) else "")
        kr_name = f.name
        break
    if kr is None:
        print(f"[multilevel] {round_label}: no children's recode (KR) with HAZ found; skipped")
        return None

    # outcome and controls
    haz = pd.to_numeric(kr[T._KDHS_VARS["haz"]], errors="coerce")
    kr["haz"] = haz.where(haz.abs() <= T._Z_VALID) / 100.0
    kr["stunted"] = (kr["haz"] < -2).where(kr["haz"].notna())
    if "hw1" in kr.columns:
        kr["child_age_months"] = pd.to_numeric(kr["hw1"], errors="coerce")
    if "b4" in kr.columns:
        kr["child_female"] = (pd.to_numeric(kr["b4"], errors="coerce") == 2).astype("float")
    if "v133" in kr.columns:
        edu = pd.to_numeric(kr["v133"], errors="coerce")
        kr["mother_edu_years"] = edu.where(edu <= 30)
    if "v190" in kr.columns:
        kr["wealth"] = pd.to_numeric(kr["v190"], errors="coerce")  # ordinal quintile 1-5
    kr["v001"] = pd.to_numeric(kr["v001"], errors="coerce").astype("Int64")

    # cluster soil index from the sampled stems
    stems = [s for s in _SOIL_STEMS if s in clusters.columns]
    if len(stems) >= 3:
        clusters["soil_index_cluster"] = clusters[stems].apply(_zscore).mean(axis=1)
    keep = (["dhsclust", "soil_index_cluster"]
            + [c for c in ("fe_isda", "rain_mm_mean", "ndvi_mean", "county_norm")
               if c in clusters.columns])
    keep = [c for c in dict.fromkeys(keep) if c in clusters.columns]
    cl = clusters[keep].rename(columns={"county_norm": "county_norm_cluster"})

    child = kr.merge(cl, left_on="v001", right_on="dhsclust", how="left")
    if "county_norm" not in child.columns or child["county_norm"].eq("").all():
        child["county_norm"] = child.get("county_norm_cluster", "")
    child["survey_round"] = round_label
    child["recode"] = kr_name
    matched = int(child["soil_index_cluster"].notna().sum()) if "soil_index_cluster" in child.columns else 0
    print(f"[multilevel] {round_label}: {len(child)} children, "
          f"{matched} matched to a cluster ({kr_name})")
    return child


def fit_multilevel(child: pd.DataFrame, round_label: str = "", soil_term: str = "fe_isda"):
    """Fit MixedLM of HAZ on the chosen cluster soil term, the cluster environment
    and child controls, with a county random intercept. Cluster covariates are
    standardized (per SD). Returns (model, tidy_coef_frame)."""
    try:
        import statsmodels.formula.api as smf
    except Exception:  # noqa: BLE001
        return None, pd.DataFrame()
    d = child.copy()
    covars = [c for c in ([soil_term] + _ENV_COVARS)
              if c in d.columns and d[c].notna().sum() > 50]
    if soil_term not in covars:
        return None, pd.DataFrame()
    for c in covars:
        d[c] = _zscore(d[c])
    controls = [c for c in _CHILD_CONTROLS if c in d.columns and d[c].notna().sum() > 50]
    terms = covars + controls
    if "haz" not in d.columns or "county_norm" not in d.columns:
        return None, pd.DataFrame()
    d = d[["haz", "county_norm"] + terms].replace([np.inf, -np.inf], np.nan).dropna()
    d = d[d["county_norm"].astype(str) != ""]
    if len(d) < 100 or d["county_norm"].nunique() < 5:
        return None, pd.DataFrame()
    formula = "haz ~ " + " + ".join(terms)
    model = smf.mixedlm(formula, data=d, groups=d["county_norm"]).fit()
    rows = []
    for k in model.params.index:
        if k in ("Intercept", "Group Var"):
            continue
        rows.append({"round": round_label, "soil_term": soil_term, "term": k,
                     "coef": round(float(model.params[k]), 4),
                     "se": round(float(model.bse[k]), 4) if k in model.bse else np.nan,
                     "p": round(float(model.pvalues[k]), 4) if k in model.pvalues else np.nan,
                     "n": int(model.nobs),
                     "n_counties": int(d["county_norm"].nunique())})
    return model, pd.DataFrame(rows)


def run(base: Path, out: Path) -> dict:
    """Build the child tables, fit the multilevel model per round and pooled, and
    write the outputs. Returns a dict of results."""
    out = Path(out)
    rounds = [("kdhs_2022", "kdhs_gps_clusters", "2022"),
              ("kdhs_2014", "kdhs_gps_clusters_2014", "2014")]
    children, coefs, summaries = {}, [], []
    for subdir, cluster_csv, label in rounds:
        child = build_child_table(base, subdir, cluster_csv, label)
        if child is None or child.empty:
            continue
        children[label] = child
        for soil_term in ("fe_isda", "soil_index_cluster"):
            model, coef = fit_multilevel(child, label, soil_term=soil_term)
            if model is None:
                continue
            tag = "iron" if soil_term == "fe_isda" else "composite"
            (out / f"multilevel_haz_{label}_{tag}.txt").write_text(
                str(model.summary()), encoding="utf-8")
            coefs.append(coef)
            if soil_term == "fe_isda":
                try:
                    v = float(model.cov_re.iloc[0, 0])
                    icc = v / (v + float(model.scale))
                    summaries.append(f"{label} (iron): n={int(model.nobs)}, "
                                     f"counties={coef['n_counties'].iloc[0]}, county ICC={icc:.3f}")
                except Exception:  # noqa: BLE001
                    summaries.append(f"{label} (iron): n={int(model.nobs)}")

    # pooled across rounds, with a round dummy
    if len(children) > 1:
        pooled = pd.concat(children.values(), ignore_index=True)
        model, coef = fit_multilevel(pooled, "pooled", soil_term="fe_isda")
        if model is not None:
            (out / "multilevel_haz_pooled_iron.txt").write_text(
                str(model.summary()), encoding="utf-8")
            coefs.append(coef)
            summaries.append(f"pooled (iron): n={int(model.nobs)}")

    coef_all = pd.concat(coefs, ignore_index=True) if coefs else pd.DataFrame()
    if not coef_all.empty:
        coef_all.to_csv(out / "multilevel_coefficients.csv", index=False)
    _write_summary(coef_all, summaries, out / "multilevel_summary.md")
    return {"coefficients": coef_all, "summaries": summaries}


def _write_summary(coef: pd.DataFrame, summaries: list, path: Path) -> None:
    lines = ["# Child-level multilevel model of HAZ", "",
             "Height-for-age on the soil and environment sampled at each child's DHS",
             "cluster, with household controls and a county random intercept. This",
             "identifies the soil-to-body link at the child's own location, net of",
             "the county socioeconomic structure that confounded the Stage 2 county",
             "regression. Coefficients on the cluster covariates are per standard",
             "deviation. Associational, not causal; survey weights are not applied",
             "in the mixed model, so read the gradients as conditional associations.", ""]
    if summaries:
        lines += ["## Fitted models", ""] + [f"- {s}" for s in summaries] + [""]
    lines += ["## Fixed effects", ""]
    lines.append(coef.to_markdown(index=False) if not coef.empty
                 else "Not available (cluster tables or recodes absent, or statsmodels missing).")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
