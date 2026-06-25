"""Policy layer: county policy-intensity panel and Policy Signal Index.

Two inputs feed the panel.

1. County agricultural expenditure from the Office of the Controller of Budget,
   County Budget Implementation Review Reports (CBIRR). Place tidy CSVs in
   data/external/cob_expenditure/ (one per fiscal year or a combined file); see
   tools/parse_cob_expenditure.py. Expected columns:
       county, fiscal_year, ag_budget_alloc_kes_m, ag_expenditure_kes_m
   optional: ag_dev_expenditure_kes_m, total_expenditure_kes_m,
             health_expenditure_kes_m, ag_absorption_rate
   This is the policy-effort backbone; until the CSVs are placed the panel still
   builds from the fertilizer rollout alone.

2. The National Fertilizer Subsidy Programme staggered rollout, bundled as a
   documented seed (kenyadb/seed/fertilizer_rollout.csv) compiled from MoALD,
   NCPB and KNTC public announcements 2023-2024, and overridable by a validated
   file in data/external/fertilizer_subsidy/.

Outputs (auto-registered by build_db):
  policy.policy_panel           county x fiscal_year long table
  policy.policy_county_summary  county cross-section with the Policy Signal Index

Everything here is descriptive. The fertilizer rollout was maize-belt-first, so
the rollout variables capture agricultural-potential targeting, which is itself a
testable contrast with nutrient need (see analysis.stage4_policy_response_model).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .crosswalk import COUNTIES, norm

SEED = Path(__file__).parent / "seed" / "fertilizer_rollout.csv"


def _county_frame(base: Path) -> pd.DataFrame:
    """Canonical 47-county keys. Prefer the crosswalk so county_code and
    county_norm match the rest of the database; fall back to the built-in list."""
    cw = base / "data" / "processed" / "crosswalk_admin.csv"
    if cw.exists():
        df = pd.read_csv(cw, dtype=str)
        if {"county_code", "county_norm"} <= set(df.columns):
            keep = [c for c in ("county_code", "county_name", "county_norm") if c in df.columns]
            out = df[keep].dropna(subset=["county_norm"])
            out = out[out["county_norm"] != ""].drop_duplicates("county_norm")
            if len(out) >= 40:
                return out.reset_index(drop=True)
    return pd.DataFrame([{"county_code": f"KE{c:02d}", "county_name": n,
                          "county_norm": norm(n)} for c, n in COUNTIES])


def _z(s: pd.Series) -> pd.Series:
    sd = s.std(ddof=0)
    return (s - s.mean()) / sd if sd else s * 0.0


def _fy_end(fy) -> int:
    """'2022/23' -> 2023; '2023/2024' -> 2024; '2023' -> 2023."""
    s = str(fy).strip()
    if "/" in s:
        a, b = s.split("/", 1)
        b = b.strip()
        if len(b) == 2:
            return int(a.strip()[:2] + b)
        try:
            return int(b)
        except ValueError:
            return -1
    try:
        return int(float(s))
    except ValueError:
        return -1


def load_fertilizer_rollout(base: Path) -> pd.DataFrame:
    """County fertilizer-subsidy entry year, pilot flag and a rollout-priority
    ordinal (pilot = 2, entered 2023 = 1, entered 2024 = 0)."""
    ext = base / "data" / "external" / "fertilizer_subsidy"
    files = sorted(ext.glob("*.csv")) if ext.exists() else []
    src = files[0] if files else (SEED if SEED.exists() else None)
    if src is None:
        return pd.DataFrame()
    df = pd.read_csv(src)
    if "county" not in df.columns:
        return pd.DataFrame()
    df["county_norm"] = df["county"].map(norm)
    for k in ("fertilizer_entered_year", "fertilizer_pilot"):
        if k not in df.columns:
            df[k] = np.nan
    pilot = pd.to_numeric(df["fertilizer_pilot"], errors="coerce").fillna(0)
    year = pd.to_numeric(df["fertilizer_entered_year"], errors="coerce")
    df["fertilizer_priority"] = np.where(pilot >= 1, 2, np.where(year <= 2023, 1, 0))
    print(f"[policy_panel] fertilizer rollout: {len(df)} counties from {src.name}")
    return df[["county_norm", "fertilizer_entered_year", "fertilizer_pilot",
               "fertilizer_priority"]]


def load_expenditure(base: Path) -> pd.DataFrame:
    """County agricultural expenditure (county x fiscal_year) from CBIRR CSVs."""
    ext = base / "data" / "external" / "cob_expenditure"
    files = sorted(ext.glob("*.csv")) if ext.exists() else []
    frames = []
    for f in files:
        d = pd.read_csv(f)
        if {"county", "fiscal_year"} <= set(d.columns):
            d["county_norm"] = d["county"].map(norm)
            frames.append(d)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    print(f"[policy_panel] expenditure: {len(out)} county-year rows from {len(frames)} file(s)")
    return out


def build_panel(base: Path):
    cf = _county_frame(base)
    fert = load_fertilizer_rollout(base)
    exp = load_expenditure(base)

    summ = cf.copy()
    if not fert.empty:
        summ = summ.merge(fert, on="county_norm", how="left")

    if not exp.empty:
        exp = exp.copy()
        exp["_fy"] = exp["fiscal_year"].map(_fy_end)
        if "ag_absorption_rate" not in exp.columns and \
           {"ag_expenditure_kes_m", "ag_budget_alloc_kes_m"} <= set(exp.columns):
            den = pd.to_numeric(exp["ag_budget_alloc_kes_m"], errors="coerce")
            exp["ag_absorption_rate"] = (pd.to_numeric(exp["ag_expenditure_kes_m"], errors="coerce")
                                         / den.where(den > 0))
        if {"total_expenditure_kes_m", "ag_expenditure_kes_m"} <= set(exp.columns):
            tot = pd.to_numeric(exp["total_expenditure_kes_m"], errors="coerce")
            exp["ag_share_of_budget"] = (pd.to_numeric(exp["ag_expenditure_kes_m"], errors="coerce")
                                         / tot.where(tot > 0))
        latest = exp.sort_values("_fy").groupby("county_norm").tail(1)
        intens = [c for c in ("ag_budget_alloc_kes_m", "ag_expenditure_kes_m",
                              "ag_dev_expenditure_kes_m", "ag_absorption_rate",
                              "ag_share_of_budget", "health_expenditure_kes_m")
                  if c in latest.columns]
        summ = summ.merge(
            latest[["county_norm", "_fy"] + intens].rename(columns={"_fy": "expenditure_fy"}),
            on="county_norm", how="left")

    # Policy Signal Index: z-score average of available effort signals.
    signals = [c for c in ("fertilizer_priority", "ag_expenditure_kes_m",
                           "ag_absorption_rate", "ag_share_of_budget") if c in summ.columns]
    if signals:
        summ["policy_signal_index"] = summ[signals].apply(_z).mean(axis=1)
        sf = [c for c in ("fertilizer_priority", "ag_expenditure_kes_m",
                          "ag_dev_expenditure_kes_m") if c in summ.columns]
        if sf:
            summ["soil_food_policy_signal"] = summ[sf].apply(_z).mean(axis=1)

    # Long panel: county x fiscal_year.
    if not exp.empty:
        panel = cf.merge(exp.drop(columns=["_fy"], errors="ignore"), on="county_norm", how="right")
        if not fert.empty:
            panel = panel.merge(fert[["county_norm", "fertilizer_entered_year"]],
                                on="county_norm", how="left")
            ey = pd.to_numeric(panel["fertilizer_entered_year"], errors="coerce")
            panel["fertilizer_active"] = (panel["fiscal_year"].map(_fy_end) >= ey).astype("Int64")
    else:
        rows = []
        for _, r in summ.iterrows():
            ey = pd.to_numeric(r.get("fertilizer_entered_year"), errors="coerce")
            for y in (2022, 2023, 2024):
                rows.append({"county_code": r["county_code"], "county_name": r["county_name"],
                             "county_norm": r["county_norm"],
                             "fiscal_year": f"{y}/{str(y + 1)[2:]}",
                             "fertilizer_active": int(not pd.isna(ey) and y >= ey)})
        panel = pd.DataFrame(rows)
    return summ, panel


def run(base: Path, *, prov=None) -> dict:
    base = Path(base)
    proc = base / "data" / "processed" / "policy"
    proc.mkdir(parents=True, exist_ok=True)
    summ, panel = build_panel(base)
    for df in (summ, panel):
        for c in df.columns:
            if df[c].dtype.kind == "f":
                df[c] = df[c].round(4)
    summ.to_csv(proc / "policy_county_summary.csv", index=False)
    panel.to_csv(proc / "policy_panel.csv", index=False)

    ext = base / "data" / "external" / "cob_expenditure"
    has_exp = ext.exists() and any(ext.glob("*.csv"))
    manifest = {
        "source_key": "policy_panel",
        "title": "Kenya county policy-intensity panel (agricultural expenditure and fertilizer subsidy rollout)",
        "publisher": ("Office of the Controller of Budget (CBIRR); Ministry of Agriculture "
                      "and Livestock Development, NCPB and KNTC (National Fertilizer Subsidy Programme)"),
        "layer": "policy",
        "fertilizer_source": "documented public rollout 2023-2024 (bundled seed) unless overridden by data/external/fertilizer_subsidy/",
        "expenditure_present": bool(has_exp),
        "counties": int(summ.shape[0]),
        "panel_rows": int(panel.shape[0]),
        "extracted_by": "Aboubacar HEMA",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "status": "ok" if not summ.empty else "manual",
    }
    (proc / "policy_panel_provenance.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (proc / "policy_county_summary_provenance.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[policy_panel] policy_county_summary ({summ.shape[0]} counties), "
          f"policy_panel ({panel.shape[0]} county-year rows); "
          f"expenditure={'yes' if has_exp else 'awaiting CBIRR CSVs'}")
    return manifest
