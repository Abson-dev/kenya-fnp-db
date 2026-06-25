"""Remote-sensing county covariates: rainfall (CHIRPS), vegetation (NDVI), and
derived drought / land-degradation indicators.

These are the climate and environment controls and instruments called for in the
empirical strategy (rainfall level and variability, drought exposure, vegetation
greenness and trend). They are produced by zonal statistics over the same county
boundaries the soil layer uses, and written to geography.remote_sensing_county.

Two input modes, both optional and combinable:

  1. Annual rasters with the year in the file name, under
     data/raw/chirps_rainfall/  (annual rainfall totals, mm)
     data/raw/modis_ndvi/       (annual mean NDVI)
     From these the module derives temporal statistics per county: mean, inter-
     annual variability, linear trend, recent standardized anomaly, and a
     standardized-anomaly drought frequency.

  2. Any named summary raster under data/external/remote_sensing/ (for example a
     Google Earth Engine export such as rainfall_mean.tif or ndvi_trend.tif),
     each zonal-averaged into a column named after the file.

Direct CHIRPS download: https://data.chc.ucsb.edu/products/CHIRPS-2.0/ (gunzip
the .tif.gz first). Practical alternative: export Kenya-clipped annual rasters
from Google Earth Engine (UCSB-CHG/CHIRPS for rainfall, MODIS/061/MOD13A2 for
NDVI) into the folders above.

Requires geopandas / rasterio / rasterstats; skips cleanly if unavailable.

Author: Aboubacar HEMA
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from .crosswalk import norm

_YEAR = re.compile(r"(?:19|20)\d{2}")


def _counties(base: Path):
    """Load the county polygons via the same content-based detection as the
    crosswalk, returning the GeoDataFrame and its name column."""
    import geopandas as gpd  # type: ignore
    from .crosswalk import find_admin_layers

    layers = find_admin_layers(base / "data" / "raw")
    adm = layers.get("adm1") or layers.get("adm2")
    if not adm:
        return None, None
    gdf = (gpd.read_file(adm["path"], layer=adm["layer"]) if adm["layer"]
           else gpd.read_file(adm["path"])).to_crs("EPSG:4326")
    name_col = adm["adm1_name"] or next(
        (c for c in gdf.columns if "name" in c.lower() or "county" in c.lower()), None)
    return gdf, name_col


def _zonal_mean(gdf, tif: Path):
    from rasterstats import zonal_stats  # type: ignore
    stats = zonal_stats(gdf, str(tif), stats=["mean"])
    return [s["mean"] for s in stats]


def _annual_series(gdf, name_col: str, src_dir: Path):
    """county_name x year DataFrame of zonal means for year-tagged rasters."""
    if not src_dir.exists():
        return None
    cols = {}
    for tif in sorted(src_dir.glob("*.tif")):
        m = _YEAR.search(tif.stem)
        if not m:
            continue
        cols[int(m.group(0))] = _zonal_mean(gdf, tif)
    if not cols:
        return None
    df = pd.DataFrame(cols, index=list(gdf[name_col].values))
    return df.reindex(sorted(df.columns), axis=1)


def _trend(values, years) -> float:
    v = np.asarray(values, dtype=float)
    y = np.asarray(years, dtype=float)
    ok = np.isfinite(v)
    if ok.sum() < 3:
        return np.nan
    return float(np.polyfit(y[ok], v[ok], 1)[0])


def _rain_features(df: pd.DataFrame) -> pd.DataFrame:
    years = list(df.columns)
    vals = df.values.astype(float)
    mean = np.nanmean(vals, axis=1)
    std = np.nanstd(vals, axis=1)
    safe = np.where(std > 0, std, np.nan)
    z = (vals - mean[:, None]) / safe[:, None]
    out = pd.DataFrame(index=df.index)
    out["rain_mm_mean"] = mean
    out["rain_cv"] = std / np.where(mean > 0, mean, np.nan)
    out["rain_trend_mm_yr"] = [_trend(r, years) for r in vals]
    out["rain_anomaly_recent"] = (vals[:, -1] - mean) / safe
    out["drought_freq"] = np.nanmean(z < -1.0, axis=1)   # share of years > 1 SD dry
    return out


def _ndvi_features(df: pd.DataFrame) -> pd.DataFrame:
    vals = df.values.astype(float)
    if np.nanmedian(np.abs(vals)) > 1.5:   # MODIS integer NDVI (scaled x10000)
        vals = vals / 10000.0
    years = list(df.columns)
    mean = np.nanmean(vals, axis=1)
    std = np.nanstd(vals, axis=1)
    out = pd.DataFrame(index=df.index)
    out["ndvi_mean"] = mean
    out["ndvi_trend"] = [_trend(r, years) for r in vals]   # negative -> browning / degradation
    out["ndvi_cv"] = std / np.where(mean > 0, mean, np.nan)
    return out


def _sidecar(base: Path, source_key: str, title: str, publisher: str,
             message: str, out_csv: Path) -> None:
    raw = out_csv.read_bytes()
    side = out_csv.with_name(f"{source_key}_provenance.json")
    side.write_text(json.dumps({
        "source_key": source_key,
        "layer": "geography",
        "title": title,
        "publisher": publisher,
        "access": "open_download",
        "csv_outputs": [out_csv.name],
        "message": message,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "extracted_at": _dt.datetime.utcnow().isoformat() + "Z",
    }, indent=2), encoding="utf-8")


def run(base: Path):
    """Build geography.remote_sensing_county from whatever rasters are present."""
    try:
        import geopandas  # noqa: F401
        import rasterstats  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"[remote_sensing] skipped: {exc} (install rasterio/rasterstats/geopandas)")
        return None

    gdf, name_col = _counties(base)
    if gdf is None or name_col is None:
        print("[remote_sensing] no county boundary layer detected under data/raw/cod_ab")
        return None

    feats = pd.DataFrame(index=list(gdf[name_col].values))
    found = []

    rain = _annual_series(gdf, name_col, base / "data" / "raw" / "chirps_rainfall")
    if rain is not None:
        feats = feats.join(_rain_features(rain))
        found.append(f"rainfall {rain.shape[1]}y ({rain.columns.min()}-{rain.columns.max()})")

    ndvi = _annual_series(gdf, name_col, base / "data" / "raw" / "modis_ndvi")
    if ndvi is not None:
        feats = feats.join(_ndvi_features(ndvi))
        found.append(f"ndvi {ndvi.shape[1]}y ({ndvi.columns.min()}-{ndvi.columns.max()})")

    ext = base / "data" / "external" / "remote_sensing"
    extra = []
    if ext.exists():
        for tif in sorted(ext.glob("*.tif")):
            col = re.sub(r"[^0-9a-z]+", "_", tif.stem.lower()).strip("_")
            feats[col] = _zonal_mean(gdf, tif)
            extra.append(col)
    if extra:
        found.append("external: " + ", ".join(extra))

    if not found:
        print("[remote_sensing] no rasters found. Place annual GeoTIFFs in "
              "data/raw/chirps_rainfall/ and data/raw/modis_ndvi/ (year in the file "
              "name), or named summary rasters in data/external/remote_sensing/.")
        return None

    feats = feats.reset_index().rename(columns={"index": "county_name"})
    feats["county_norm"] = feats["county_name"].map(norm)
    # attach county_code from the crosswalk where available
    xpath = base / "data" / "processed" / "crosswalk_admin.csv"
    if xpath.exists():
        xw = pd.read_csv(xpath).drop_duplicates("county_norm")[["county_norm", "county_code"]]
        feats = feats.merge(xw, on="county_norm", how="left")
    lead = [c for c in ("county_code", "county_name", "county_norm") if c in feats.columns]
    feats = feats[lead + [c for c in feats.columns if c not in lead]]

    out = base / "data" / "processed" / "geography" / "remote_sensing_county.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    feats.round(4).to_csv(out, index=False)

    if rain is not None:
        _sidecar(base, "chirps_rainfall", "CHIRPS 2.0 rainfall (county)",
                 "UCSB Climate Hazards Group",
                 f"county zonal rainfall features from {rain.shape[1]} annual rasters", out)
    if ndvi is not None:
        _sidecar(base, "modis_ndvi", "MODIS NDVI (county)", "NASA LP DAAC",
                 f"county zonal NDVI features from {ndvi.shape[1]} annual rasters", out)

    print(f"[remote_sensing] geography.remote_sensing_county ({len(feats)} counties; "
          f"{'; '.join(found)}) -> {out.name}")
    return out
