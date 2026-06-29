"""DHS GPS clusters: geocoded survey cluster centroids joined to counties and to
the local soil, rainfall and vegetation rasters.

This builds the cluster-level covariate table a child-level multilevel model
needs: each DHS cluster is linked to the soil, rainfall and NDVI at its own
displaced location rather than to a county average. The DHS child recodes join to
this table on the cluster id (the children-recode v001 equals DHSCLUST).

Input: the DHS GPS Datasets point shapefile (for example KEGE81FL.shp for 2022,
KEGE71FL.shp for 2014), placed anywhere under data/external/<subdir>/. Clusters
with missing coordinates (latitude and longitude both zero) are dropped.

Output: data/processed/health/<out_name>.csv (registered as health.<out_name>)
with the cluster id, survey round, latitude, longitude, urban or rural flag, the
county keys, and point-sampled rainfall, NDVI, soil moisture, erosion and
SoilGrids or iSDA values for whatever rasters are present.

Requires geopandas and rasterio; skips cleanly if they or the GPS file are
absent.

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

from .crosswalk import find_admin_layers, norm

# SoilGrids and iSDA topsoil property stems worth sampling at each cluster.
_SOIL_STEMS = ("soc", "nitrogen", "cec", "phh2o",
               "p_isda", "k_isda", "zn_isda", "fe_isda")


def _slug(stem: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", stem.lower()).strip("_")


def _find_gps(base: Path, subdir: str):
    """Locate the DHS GPS point shapefile under data/external|raw/<subdir>/."""
    import geopandas as gpd  # type: ignore
    for parent in ("external", "raw"):
        d = base / "data" / parent / subdir
        if not d.exists():
            continue
        for shp in sorted(d.rglob("*.shp")):
            try:
                head = gpd.read_file(shp, rows=1)
            except Exception:  # noqa: BLE001
                continue
            cols = {c.upper() for c in head.columns}
            if {"LATNUM", "LONGNUM"} <= cols or "DHSCLUST" in cols:
                return shp
    return None


def _counties_gdf(base: Path):
    import geopandas as gpd  # type: ignore
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
    if adm["count"] > 60:
        g = g.dissolve(by="county_norm", as_index=False)
    return g


def _sample_one(coords, path: Path):
    import rasterio  # type: ignore
    with rasterio.open(path) as src:
        vals = np.array([v[0] for v in src.sample(coords)], dtype="float64")
        nod = src.nodata
    if nod is not None:
        vals[vals == nod] = np.nan
    vals[np.abs(vals) > 1e19] = np.nan
    return vals


def _annual_mean(coords, src_dir: Path, rescale_ndvi: bool = False):
    """Mean across year-tagged rasters of the value sampled at each point."""
    if not src_dir.exists():
        return None
    stacks = []
    for tif in sorted(src_dir.glob("*.tif")):
        if not re.search(r"(?:19|20)\d{2}", tif.stem):
            continue
        stacks.append(_sample_one(coords, tif))
    if not stacks:
        return None
    arr = np.nanmean(np.vstack(stacks), axis=0)
    if rescale_ndvi and np.nanmedian(np.abs(arr)) > 1.5:
        arr = arr / 10000.0
    return arr


def _soil_rasters(base: Path) -> dict:
    """One topsoil raster per soil property for cluster sampling. SoilGrids files
    are matched by stem (soc, nitrogen, cec, phh2o); iSDA files are named by their
    real column (isda_iron_extractable.tif -> fe_isda) so that the micronutrients,
    iron in particular, are sampled at each cluster and not silently dropped."""
    out = {}
    # SoilGrids topsoil grids, matched by stem
    sg = base / "data" / "raw" / "soilgrids"
    if sg.exists():
        tifs = sorted(sg.glob("*.tif"))
        for stem in ("soc", "nitrogen", "cec", "phh2o"):
            for tif in tifs:
                low = tif.stem.lower()
                if low == stem or low.startswith(stem + "_") or f"_{stem}" in low:
                    out.setdefault(stem, tif)
                    break
    # iSDA grids, named by the same mapping the county transform uses
    from . import transforms as T
    for sub in ("isda", "isda_soil"):
        d = base / "data" / "raw" / sub
        if not d.exists():
            continue
        for tif in sorted(d.glob("*.tif")):
            out.setdefault(T._isda_colname(tif.stem), tif)
    return out


def run(base: Path, subdir: str = "kdhs_2022", out_name: str = "kdhs_gps_clusters",
        round_label: str = "2022", prov=None):
    """Build health.<out_name> from the DHS GPS shapefile under data/.../<subdir>.
    Returns the output path, or None when geopandas, rasterio or the GPS file is
    absent."""
    try:
        import geopandas as gpd  # type: ignore  # noqa: F401
        import rasterio  # type: ignore  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"[dhs_gps] skipped: {exc} (install geopandas/rasterio)")
        return None

    shp = _find_gps(base, subdir)
    if shp is None:
        print(f"[dhs_gps] {out_name}: no GPS shapefile under data/external/{subdir}/ "
              "(download the DHS GPS Datasets); skipped")
        return None

    pts = gpd.read_file(shp).to_crs("EPSG:4326")
    pts.columns = [c.upper() if c.upper() in
                   {"DHSCLUST", "LATNUM", "LONGNUM", "URBAN_RURA", "ADM1NAME", "DHSREGNA"}
                   else c for c in pts.columns]
    # drop clusters with missing coordinates (DHS encodes these as 0, 0)
    if {"LATNUM", "LONGNUM"} <= set(pts.columns):
        pts = pts[~((pts["LATNUM"] == 0) & (pts["LONGNUM"] == 0))].copy()
    pts = pts[pts.geometry.notna() & ~pts.geometry.is_empty].copy()
    if pts.empty:
        print(f"[dhs_gps] {out_name}: no clusters with valid coordinates; skipped")
        return None

    out = pd.DataFrame()
    out["dhsclust"] = pts["DHSCLUST"].astype("Int64") if "DHSCLUST" in pts.columns \
        else range(1, len(pts) + 1)
    out["survey_round"] = round_label
    out["latitude"] = pts.geometry.y.values
    out["longitude"] = pts.geometry.x.values
    if "URBAN_RURA" in pts.columns:
        out["urban"] = (pts["URBAN_RURA"].astype(str).str.upper().str[0] == "U").astype(int).values

    # county assignment by spatial join to the boundary
    counties = _counties_gdf(base)
    if counties is not None:
        joined = gpd.sjoin(pts[["geometry"]].reset_index(drop=True),
                           counties[["county_name", "county_norm", "geometry"]],
                           how="left", predicate="within")
        joined = joined[~joined.index.duplicated(keep="first")]
        out["county_name"] = joined["county_name"].values
        out["county_norm"] = joined["county_norm"].values
        # attach county_code from the crosswalk
        xpath = base / "data" / "processed" / "crosswalk_admin.csv"
        if xpath.exists():
            xw = (pd.read_csv(xpath, dtype=str).drop_duplicates("county_norm")
                  [["county_norm", "county_code"]])
            out = out.merge(xw, on="county_norm", how="left")
    elif "ADM1NAME" in pts.columns:
        out["county_name"] = pts["ADM1NAME"].values
        out["county_norm"] = pts["ADM1NAME"].map(norm).values

    # point-sample the local rasters
    coords = list(zip(out["longitude"], out["latitude"]))
    sampled = []
    rs_dir = base / "data" / "external" / "remote_sensing"
    if rs_dir.exists():
        for tif in sorted(rs_dir.glob("*.tif")):
            out[_slug(tif.stem)] = _sample_one(coords, tif)
            sampled.append(_slug(tif.stem))
    rain = _annual_mean(coords, base / "data" / "raw" / "chirps_rainfall")
    if rain is not None:
        out["rain_mm_mean"] = rain
        sampled.append("rain_mm_mean")
    ndvi = _annual_mean(coords, base / "data" / "raw" / "modis_ndvi", rescale_ndvi=True)
    if ndvi is not None:
        out["ndvi_mean"] = ndvi
        sampled.append("ndvi_mean")
    for stem, tif in _soil_rasters(base).items():
        out[stem] = _sample_one(coords, tif)
        sampled.append(stem)

    lead = [c for c in ("dhsclust", "survey_round", "latitude", "longitude", "urban",
                        "county_code", "county_name", "county_norm") if c in out.columns]
    out = out[lead + [c for c in out.columns if c not in lead]]

    dest = base / "data" / "processed" / "health" / f"{out_name}.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    out.round(5).to_csv(dest, index=False)

    raw = dest.read_bytes()
    side = dest.with_name(f"kdhs_gps_{round_label}_provenance.json")
    side.write_text(json.dumps({
        "source_key": f"kdhs_gps_{round_label}",
        "layer": "health",
        "title": f"Kenya DHS {round_label} GPS clusters (geocoded, county-joined)",
        "publisher": "KNBS and ICF (DHS Program)",
        "access": "gated_download",
        "csv_outputs": [dest.name],
        "message": (f"{len(out)} clusters with valid coordinates; "
                    f"sampled: {', '.join(sampled) if sampled else 'none'}"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "extracted_at": _dt.datetime.utcnow().isoformat() + "Z",
    }, indent=2), encoding="utf-8")

    ncounty = int(out["county_norm"].notna().sum()) if "county_norm" in out.columns else 0
    print(f"[dhs_gps] health.{out_name} ({len(out)} clusters, {ncounty} county-assigned; "
          f"sampled {len(sampled)} covariates) -> {dest.name}")
    return dest
