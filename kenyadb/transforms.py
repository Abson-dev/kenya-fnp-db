"""Normalisation transforms: raw / external files -> processed layer tables.

Each transform reads what an acquisition handler (or a manual drop) produced,
tidies it, joins to the master crosswalk where the data is sub-national, and
writes data/processed/<layer>/<name>.csv. The build step then registers each
output as <layer>.<name>. Every transform skips gracefully when its input is
absent, so this module can run at any point as more sources arrive.

Author: Aboubacar HEMA
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import pandas as pd

from .crosswalk import COUNTIES, norm


def _out(base: Path, layer: str, name: str) -> Path:
    d = base / "data" / "processed" / layer
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.csv"


def _crosswalk(base: Path) -> pd.DataFrame | None:
    p = base / "data" / "processed" / "crosswalk_admin.csv"
    return pd.read_csv(p, dtype=str) if p.exists() else None


# --- health: World Bank HNP JSON -> tidy country-year panel -----------------
def wb_hnp_panel(base: Path) -> Path | None:
    raw = base / "data" / "raw" / "wb_hnp"
    files = sorted(raw.glob("*.json"))
    if not files:
        return None
    rows = []
    for f in files:
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
            series = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
        except Exception:  # noqa: BLE001
            continue
        for obs in series or []:
            if obs.get("value") is None:
                continue
            rows.append({
                "indicator": obs.get("indicator", {}).get("id"),
                "indicator_name": obs.get("indicator", {}).get("value"),
                "country": obs.get("country", {}).get("id"),
                "year": int(obs["date"]),
                "value": obs["value"],
            })
    if not rows:
        return None
    df = pd.DataFrame(rows).sort_values(["indicator", "year"])
    out = _out(base, "health", "wb_hnp_panel")
    df.to_csv(out, index=False)
    print(f"[transform] health.wb_hnp_panel ({len(df)} rows) -> {out.name}")
    return out


# --- food: filter FAOSTAT normalised zips to Kenya --------------------------
def faostat_kenya(base: Path, area_code: int = 114) -> Path | None:
    raw = base / "data" / "raw" / "faostat"
    zips = sorted(raw.glob("*_normalized.zip"))
    if not zips:
        return None
    frames = []
    for z in zips:
        try:
            df = pd.read_csv(z, compression="zip", encoding="latin-1", low_memory=False)
        except Exception:  # noqa: BLE001
            continue
        col = next((c for c in df.columns if c.lower() in ("area code", "area_code")), None)
        if col is None:
            continue
        ken = df[df[col] == area_code].copy()
        ken["faostat_domain"] = z.stem.replace("_normalized", "")
        frames.append(ken)
    if not frames:
        return None
    out_df = pd.concat(frames, ignore_index=True)
    out = _out(base, "food", "faostat_kenya")
    out_df.to_csv(out, index=False)
    print(f"[transform] food.faostat_kenya ({len(out_df)} rows) -> {out.name}")
    return out


# --- food: WFP + World Bank prices into separate but linkable tables --------
def prices(base: Path) -> list[Path]:
    written = []
    wfp_dir = base / "data" / "raw" / "wfp_prices"
    wfp_csv = next(iter(sorted(wfp_dir.glob("*.csv"))), None) if wfp_dir.exists() else None
    if wfp_csv is not None:
        try:
            df = pd.read_csv(wfp_csv, low_memory=False)
            out = _out(base, "food", "prices_wfp_observed")
            df.to_csv(out, index=False)
            written.append(out)
            print(f"[transform] food.prices_wfp_observed ({len(df)} rows) -> {out.name}")
        except Exception as exc:  # noqa: BLE001
            print(f"[transform] wfp prices skipped: {exc}")

    wb_dir = base / "data" / "raw" / "wb_rtfp"
    wb_csv = next(iter(sorted(wb_dir.glob("*.csv"))), None) if wb_dir.exists() else None
    if wb_csv is not None:
        try:
            df = pd.read_csv(wb_csv, low_memory=False)
            ccol = next((c for c in df.columns if c.lower() in ("iso3", "country", "adm0_code")), None)
            if ccol is not None:
                df = df[df[ccol].astype(str).str.upper().str.contains("KEN")]
            out = _out(base, "food", "prices_wb_modeled")
            df.to_csv(out, index=False)
            written.append(out)
            print(f"[transform] food.prices_wb_modeled ({len(df)} rows) -> {out.name}")
        except Exception as exc:  # noqa: BLE001
            print(f"[transform] wb prices skipped: {exc}")
    return written


# --- soil: SoilGrids zonal statistics by county -----------------------------
def soilgrids_zonal(base: Path) -> Path | None:
    """Zonal mean of each SoilGrids coverage per county polygon. Requires the
    COD-AB admin layer and rasterio/rasterstats; skips cleanly otherwise."""
    tifs = sorted((base / "data" / "raw" / "soilgrids").glob("*.tif"))
    cod_dir = base / "data" / "raw" / "cod_ab"
    if not tifs or not cod_dir.exists():
        return None
    try:
        import geopandas as gpd  # type: ignore
        from rasterstats import zonal_stats  # type: ignore
    except Exception:  # noqa: BLE001
        print("[transform] soilgrids_zonal skipped: geopandas/rasterstats not installed")
        return None
    adm1 = next(iter(glob.glob(str(cod_dir / "**" / "*adm1*.shp"), recursive=True)
                      + glob.glob(str(cod_dir / "**" / "*ADM1*.shp"), recursive=True)), None)
    if adm1 is None:
        return None
    gdf = gpd.read_file(adm1).to_crs("EPSG:4326")
    name_col = next((c for c in gdf.columns if "adm1" in c.lower() and "name" in c.lower()), None)
    result = gdf[[name_col]].rename(columns={name_col: "county_name"}).copy()
    for tif in tifs:
        stats = zonal_stats(gdf, str(tif), stats=["mean"], nodata=-32768)
        result[tif.stem] = [s["mean"] for s in stats]
    result["county_norm"] = result["county_name"].map(norm)
    out = _out(base, "soil", "soilgrids_zonal_county")
    result.to_csv(out, index=False)
    print(f"[transform] soil.soilgrids_zonal_county ({len(result)} rows) -> {out.name}")
    return out


def run_all(base: Path) -> None:
    print("[transform] running normalisation transforms")
    wb_hnp_panel(base)
    faostat_kenya(base)
    prices(base)
    soilgrids_zonal(base)
    print("[transform] done (missing inputs were skipped)")
