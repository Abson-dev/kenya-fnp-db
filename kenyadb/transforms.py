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
import re
from pathlib import Path

import pandas as pd
import numpy as np
import yaml

from .crosswalk import COUNTIES, norm


def _out(base: Path, layer: str, name: str) -> Path:
    d = base / "data" / "processed" / layer
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.csv"


def _crosswalk(base: Path) -> pd.DataFrame | None:
    p = base / "data" / "processed" / "crosswalk_admin.csv"
    return pd.read_csv(p, dtype=str) if p.exists() else None


# --- generic ingester for manual / gated drops ------------------------------
# Sources owned by a dedicated transform (skip here to avoid duplicate tables).
_DEDICATED = {"wb_rtfp", "wfp_prices", "kdhs_2022"}


def _attach_county(df: pd.DataFrame, xwalk: pd.DataFrame | None) -> pd.DataFrame:
    """If df has a column whose values look like Kenyan counties, attach the
    canonical county_code / county_name from the crosswalk. Non-destructive:
    returns df unchanged when no county column is detected."""
    if xwalk is None or df.empty:
        return df
    counties = set(xwalk["county_norm"]) - {""}
    best_col, best_hits = None, 0
    for c in df.columns:
        try:
            vals = df[c].dropna().astype(str).map(norm)
        except Exception:  # noqa: BLE001
            continue
        hits = vals.isin(counties).sum()
        if hits > best_hits:
            best_col, best_hits = c, hits
    # require at least a third of rows (or 20 rows) to match before joining
    if best_col is None or best_hits < min(20, max(1, len(df) // 3)):
        return df
    cmap = (xwalk.drop_duplicates("county_norm")
            .set_index("county_norm")[["county_code", "county_name"]])
    keys = df[best_col].astype(str).map(norm)
    df = df.copy()
    df["county_code"] = keys.map(cmap["county_code"])
    df["county_name"] = keys.map(cmap["county_name"])
    return df


def _route_layer(source: str, layer_of: dict) -> str:
    """Map an external folder name to its layer, tolerating small naming
    differences (e.g. folder 'fortification' -> key 'fortification_refs')."""
    if source in layer_of:
        return layer_of[source]
    s = re.sub(r"[^a-z0-9]", "", source.lower())
    for k, layer in layer_of.items():
        ks = re.sub(r"[^a-z0-9]", "", k.lower())
        if s and (s == ks or ks.startswith(s) or s.startswith(ks)):
            return layer
    return "core"


def ingest_external(base: Path, config_path: Path | None = None) -> list[Path]:
    """Ingest every CSV / XLSX dropped under data/external/<source>/ into the
    right layer schema, joining to the crosswalk when a county column is found.

    This is what makes the manual gated drops usable: obtain a dashboard export
    or county fact-sheet table, drop it in data/external/<source>/, and it
    becomes <layer>.<source>__<file> on the next build. Survey microdata
    (.DTA / .SAV) is intentionally NOT handled here - those need dedicated
    survey transforms, not a raw dump.
    """
    cfg_path = config_path or (base / "config" / "sources.yaml")
    try:
        cfg = yaml.safe_load(open(cfg_path, encoding="utf-8"))
        layer_of = {k: layer for layer, srcs in cfg["layers"].items() for k in srcs}
    except Exception:  # noqa: BLE001
        layer_of = {}

    ext = base / "data" / "external"
    if not ext.exists():
        return []
    xwalk = _crosswalk(base)
    written: list[Path] = []
    for sdir in sorted(p for p in ext.iterdir() if p.is_dir()):
        source = sdir.name
        if source in _DEDICATED:
            continue
        layer = _route_layer(source, layer_of)
        files = sorted(sdir.rglob("*.csv")) + sorted(sdir.rglob("*.xlsx"))
        for f in files:
            try:
                df = (pd.read_excel(f) if f.suffix == ".xlsx"
                      else pd.read_csv(f, low_memory=False))
            except Exception as exc:  # noqa: BLE001
                print(f"[ingest] could not read external/{source}/{f.name}: {exc}")
                continue
            df = _attach_county(df, xwalk)
            stem = re.sub(r"[^0-9a-zA-Z]+", "_", f.stem).strip("_").lower()
            out = _out(base, layer, f"{source}__{stem}")
            df.to_csv(out, index=False)
            written.append(out)
            joined = "county-joined" if "county_code" in df.columns else "no county join"
            print(f"[ingest] {layer}.{source}__{stem} ({len(df)} rows, {joined}) "
                  f"<- external/{source}/{f.name}")
    return written


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
    """Filter each FAOSTAT normalised zip to Kenya (area code 114).

    A FAOSTAT zip holds the main data CSV plus small Flags/ItemGroup metadata
    CSVs, so we open the archive and read the largest member (the data file)
    rather than assuming the first entry.
    """
    import zipfile

    raw = base / "data" / "raw" / "faostat"
    zips = sorted(raw.glob("*_normalized.zip"))
    if not zips:
        return None
    frames = []
    for z in zips:
        try:
            with zipfile.ZipFile(z) as zf:
                members = [m for m in zf.infolist() if m.filename.lower().endswith(".csv")]
                if not members:
                    continue
                main = max(members, key=lambda m: m.file_size)
                with zf.open(main) as fh:
                    df = pd.read_csv(fh, encoding="latin-1", low_memory=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[transform] faostat: could not read {z.name}: {exc}")
            continue
        col = next((c for c in df.columns
                    if c.lower().replace(" ", "_") in ("area_code", "areacode")), None)
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
    print(f"[transform] food.faostat_kenya ({len(out_df)} rows, {len(frames)} domains) -> {out.name}")
    return out


# --- food: WFP + World Bank prices into separate but linkable tables --------
def _first_table(base: Path, source: str):
    """Return (DataFrame, path) for the first csv/xlsx found for `source`,
    searching both data/raw/<source>/ and the manual data/external/<source>/."""
    for parent in ("raw", "external"):
        d = base / "data" / parent / source
        if not d.exists():
            continue
        for f in sorted(d.rglob("*.csv")) + sorted(d.rglob("*.xlsx")):
            try:
                df = (pd.read_excel(f) if f.suffix == ".xlsx"
                      else pd.read_csv(f, low_memory=False))
                return df, f
            except Exception as exc:  # noqa: BLE001
                print(f"[transform] could not read {f.name}: {exc}")
    return None, None


def _strip_hxl(df: pd.DataFrame) -> pd.DataFrame:
    """Drop a leading HXL tag row if present. WFP HDX CSVs put machine tags
    (#date, #adm1+name, #value+price ...) in the first data row, which would
    otherwise become a bogus observation and force numeric columns to text."""
    if df.empty:
        return df
    first = df.iloc[0].astype(str)
    if (first.str.startswith("#").mean() > 0.5):
        df = df.iloc[1:].reset_index(drop=True)
    return df


def _coerce(df: pd.DataFrame, numeric=(), dates=()) -> pd.DataFrame:
    for c in numeric:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in dates:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


def _assign_county_by_point(df: pd.DataFrame, base: Path) -> pd.DataFrame:
    """Assign each row to a county by point-in-polygon on its lat/lon, using the
    COD-AB county layer. This is how WFP/WB market prices (tagged by province,
    not county) get a correct county key. Rows without coordinates, or all rows
    if geopandas/boundaries are unavailable, are returned with empty county
    fields rather than failing."""
    lat = next((c for c in df.columns if c.lower() in ("latitude", "lat")), None)
    lon = next((c for c in df.columns if c.lower() in ("longitude", "lon", "lng")), None)
    if lat is None or lon is None:
        return df
    try:
        import geopandas as gpd  # type: ignore
        from .crosswalk import find_admin_layers
    except Exception:  # noqa: BLE001
        return df
    layers = find_admin_layers(base / "data" / "raw")
    adm = layers.get("adm1") or layers.get("adm2")
    if not adm:
        print("[transform] prices: no county layer for spatial join; leaving county blank")
        return df
    counties = (gpd.read_file(adm["path"], layer=adm["layer"]) if adm["layer"]
                else gpd.read_file(adm["path"])).to_crs("EPSG:4326")
    name_col = adm["adm1_name"] or next(
        (c for c in counties.columns if "name" in c.lower() or "county" in c.lower()), None)
    if name_col is None:
        return df
    df = df.copy()
    coords = df[[lon, lat]].apply(pd.to_numeric, errors="coerce")
    valid = coords.notna().all(axis=1)
    pts = gpd.GeoDataFrame(
        df.loc[valid].copy(),
        geometry=gpd.points_from_xy(coords.loc[valid, lon], coords.loc[valid, lat]),
        crs="EPSG:4326",
    ).to_crs(32737)  # UTM 37S for a stable nearest-county assignment
    cty = counties.to_crs(32737)[[name_col, "geometry"]]
    joined = gpd.sjoin_nearest(pts, cty, how="left", max_distance=20000)
    joined = joined[~joined.index.duplicated(keep="first")]
    df["county_name"] = ""
    df.loc[joined.index, "county_name"] = joined[name_col].astype(str).values
    df["county_norm"] = df["county_name"].map(lambda x: norm(x) if isinstance(x, str) else "")
    matched = (df["county_norm"] != "").sum()
    print(f"[transform] prices: spatial county assignment matched {matched}/{len(df)} rows")
    return df


def _attach_county_code(df: pd.DataFrame, base: Path) -> pd.DataFrame:
    """Add canonical county_code from the crosswalk where county_norm is set."""
    xwalk = _crosswalk(base)
    if xwalk is None or "county_norm" not in df.columns:
        return df
    cmap = (xwalk.drop_duplicates("county_norm").set_index("county_norm")["county_code"])
    df = df.copy()
    df["county_code"] = df["county_norm"].map(cmap)
    return df


def prices(base: Path) -> list[Path]:
    written = []

    wfp_df, wfp_f = _first_table(base, "wfp_prices")
    if wfp_df is not None:
        wfp_df = _strip_hxl(wfp_df)
        wfp_df = _coerce(wfp_df,
                         numeric=("price", "usdprice", "latitude", "longitude"),
                         dates=("date",))
        wfp_df = _assign_county_by_point(wfp_df, base)
        wfp_df = _attach_county_code(wfp_df, base)
        out = _out(base, "food", "prices_wfp_observed")
        wfp_df.to_csv(out, index=False)
        written.append(out)
        print(f"[transform] food.prices_wfp_observed ({len(wfp_df)} rows, {wfp_f.name}) -> {out.name}")

    wb_df, wb_f = _first_table(base, "wb_rtfp")
    if wb_df is not None:
        wb_df = _strip_hxl(wb_df)
        ccol = next((c for c in wb_df.columns
                     if c.lower() in ("iso3", "country", "adm0_code", "countryiso3")), None)
        if ccol is not None:
            wb_df = wb_df[wb_df[ccol].astype(str).str.upper().str.contains("KEN")]
        wb_df = _coerce(wb_df, numeric=("Open", "High", "Low", "Close", "Inflation"),
                        dates=("date",))
        wb_df = _assign_county_by_point(wb_df, base)
        wb_df = _attach_county_code(wb_df, base)
        out = _out(base, "food", "prices_wb_modeled")
        wb_df.to_csv(out, index=False)
        written.append(out)
        print(f"[transform] food.prices_wb_modeled ({len(wb_df)} rows, {wb_f.name}) -> {out.name}")
    return written


# --- soil: SoilGrids zonal statistics by county -----------------------------
def soilgrids_zonal(base: Path) -> Path | None:
    """Zonal mean of each SoilGrids coverage per county polygon. Uses the same
    content-based boundary detection as the crosswalk, so it no longer depends
    on COD-AB file naming. Requires rasterio/rasterstats; skips cleanly otherwise."""
    tifs = sorted((base / "data" / "raw" / "soilgrids").glob("*.tif"))
    if not tifs:
        return None
    try:
        import geopandas as gpd  # type: ignore
        from rasterstats import zonal_stats  # type: ignore
        from .crosswalk import find_admin_layers
    except Exception as exc:  # noqa: BLE001
        print(f"[transform] soilgrids_zonal skipped: {exc} (install rasterstats/rasterio)")
        return None

    layers = find_admin_layers(base / "data" / "raw")
    adm1 = layers.get("adm1") or layers.get("adm2")  # prefer counties, fall back
    if not adm1:
        print("[transform] soilgrids_zonal: no county boundary layer detected under "
              "data/raw/cod_ab (need ~47 features)")
        return None

    gdf = (gpd.read_file(adm1["path"], layer=adm1["layer"]) if adm1["layer"]
           else gpd.read_file(adm1["path"])).to_crs("EPSG:4326")
    name_col = adm1["adm1_name"] or next(
        (c for c in gdf.columns if "name" in c.lower() or "county" in c.lower()), None)
    if name_col is None:
        print("[transform] soilgrids_zonal: county name column not found")
        return None

    result = gdf[[name_col]].rename(columns={name_col: "county_name"}).copy()
    for tif in tifs:
        stats = zonal_stats(gdf, str(tif), stats=["mean"], nodata=-32768)
        result[tif.stem] = [s["mean"] for s in stats]
    result["county_norm"] = result["county_name"].map(norm)
    out = _out(base, "soil", "soilgrids_zonal_county")
    result.to_csv(out, index=False)
    print(f"[transform] soil.soilgrids_zonal_county ({len(result)} rows, "
          f"{len(tifs)} coverages) -> {out.name}")
    return out


# --- soil: AfSIS soil-chemistry points -> county micronutrient means ---------
# SoilGrids supplies macro-properties (SOC, N, pH, CEC, texture) but not the
# extractable micronutrients central to the soil-food-body pathway. AfSIS wet
# chemistry provides P, K, Zn, Fe (and others) as georeferenced points, which
# this transform averages to county level by a point-in-polygon spatial join.
_AFSIS_TARGETS = {
    "P":  ["p", "phosphorus", "mehlichp", "m3p", "pppm", "extractablep"],
    "K":  ["k", "potassium", "mehlichk", "m3k", "kppm"],
    "Zn": ["zn", "zinc"],
    "Fe": ["fe", "iron"],
    "Ca": ["ca", "calcium"],
    "Mg": ["mg", "magnesium"],
    "S":  ["s", "sulphur", "sulfur"],
    "Cu": ["cu", "copper"],
    "Mn": ["mn", "manganese"],
    "B":  ["b", "boron"],
}
_AFSIS_LAT = ["lat", "latitude", "ycoord", "gpslat", "ylat", "y"]
_AFSIS_LON = ["lon", "long", "longitude", "xcoord", "gpslon", "xlon", "x"]


def _afsis_clean(c) -> str:
    return re.sub(r"[^0-9a-z]+", "", str(c).lower())


def _afsis_coord(cols, aliases):
    cl = {_afsis_clean(c): c for c in cols}
    for a in aliases:
        if a in cl:
            return cl[a]
    for cc, orig in cl.items():
        if any(cc.endswith(a) for a in aliases):
            return orig
    return None


def afsis_county_chem(base: Path) -> Path | None:
    """County mean of AfSIS extractable nutrients (P, K, Zn, Fe and others) by a
    point-in-polygon join to the same county boundaries the soil raster layer
    uses. Coordinate and nutrient columns are auto-detected so the transform
    tolerates the differing AfSIS layouts. Skips cleanly without geopandas."""
    csvs = sorted((base / "data" / "raw" / "afsis_chem").rglob("*.csv"))
    if not csvs:
        return None
    try:
        import geopandas as gpd  # type: ignore
        from .crosswalk import find_admin_layers
    except Exception as exc:  # noqa: BLE001
        print(f"[transform] afsis_county_chem skipped: {exc} (install geopandas)")
        return None

    layers = find_admin_layers(base / "data" / "raw")
    adm = layers.get("adm1") or layers.get("adm2")
    if not adm:
        print("[transform] afsis_county_chem: no county boundary layer detected under "
              "data/raw/cod_ab (need ~47 features)")
        return None
    counties = (gpd.read_file(adm["path"], layer=adm["layer"]) if adm["layer"]
                else gpd.read_file(adm["path"])).to_crs("EPSG:4326")
    name_col = adm["adm1_name"] or next(
        (c for c in counties.columns if "name" in c.lower() or "county" in c.lower()), None)
    if name_col is None:
        print("[transform] afsis_county_chem: county name column not found")
        return None

    frames = []
    for f in csvs:
        try:
            frames.append(pd.read_csv(f))
        except Exception:  # noqa: BLE001
            continue
    if not frames:
        return None
    pts = pd.concat(frames, ignore_index=True, sort=False)

    latc = _afsis_coord(pts.columns, _AFSIS_LAT)
    lonc = _afsis_coord(pts.columns, _AFSIS_LON)
    if not latc or not lonc:
        print(f"[transform] afsis_county_chem: latitude/longitude columns not found "
              f"(lat={latc}, lon={lonc}); rename or add coordinate columns")
        return None

    cl = {_afsis_clean(c): c for c in pts.columns}
    colmap = {}
    for canon, aliases in _AFSIS_TARGETS.items():
        for a in aliases:
            if a in cl:
                colmap[canon] = cl[a]
                break
    if not colmap:
        print("[transform] afsis_county_chem: no recognised nutrient columns "
              "(looked for P, K, Zn, Fe, Ca, Mg, S, Cu, Mn, B)")
        return None

    keep = pts[[latc, lonc, *colmap.values()]].copy()
    for canon, src in colmap.items():
        keep[f"{canon.lower()}_afsis"] = pd.to_numeric(keep[src], errors="coerce")
    keep[latc] = pd.to_numeric(keep[latc], errors="coerce")
    keep[lonc] = pd.to_numeric(keep[lonc], errors="coerce")
    keep = keep.dropna(subset=[latc, lonc])
    keep = keep[keep[latc].between(-90, 90) & keep[lonc].between(-180, 180)]
    if keep.empty:
        print("[transform] afsis_county_chem: no valid coordinates after cleaning")
        return None

    gpts = gpd.GeoDataFrame(keep, geometry=gpd.points_from_xy(keep[lonc], keep[latc]),
                            crs="EPSG:4326")
    joined = gpd.sjoin(gpts, counties[[name_col, "geometry"]], how="inner", predicate="within")
    if joined.empty:
        print("[transform] afsis_county_chem: no AfSIS points fell within Kenya county polygons")
        return None

    nutr = [f"{c.lower()}_afsis" for c in colmap]
    agg = joined.groupby(name_col)[nutr].mean()
    cnt = joined.groupby(name_col).size().rename("n_afsis_points")
    result = agg.join(cnt).reset_index().rename(columns={name_col: "county_name"})
    result["county_norm"] = result["county_name"].map(norm)
    result = result[["county_name", "county_norm", "n_afsis_points", *nutr]]
    out = _out(base, "soil", "afsis_county")
    result.round(4).to_csv(out, index=False)
    print(f"[transform] soil.afsis_county ({len(result)} counties, {len(nutr)} nutrients "
          f"[{', '.join(colmap)}], {int(cnt.sum())} points) -> {out.name}")
    return out


# --- soil: iSDAsoil gridded nutrients -> county means ------------------------
# iSDAsoil provides continent-wide gridded predictions of extractable P, K, Zn,
# Fe (and more), which fills the micronutrient gap SoilGrids leaves and the
# sparse, sentinel-site coverage of the AfSIS points. The Kenya rasters are
# produced by tools/download_isda_gee.py (back-transformed to natural units and
# blended to 0-30 cm); this transform averages them to county.
_ISDA_RENAME = {
    "phosphorus_extractable": "p_isda", "potassium_extractable": "k_isda",
    "zinc_extractable": "zn_isda", "iron_extractable": "fe_isda",
    "calcium_extractable": "ca_isda", "magnesium_extractable": "mg_isda",
    "sulphur_extractable": "s_isda", "nitrogen_total": "n_isda",
    "carbon_organic": "soc_isda", "ph": "ph_isda",
}


def _isda_colname(stem: str) -> str:
    s = stem.lower()
    if s.startswith("isda_"):
        s = s[5:]
    if s in _ISDA_RENAME:
        return _ISDA_RENAME[s]
    s = re.sub(r"[^0-9a-z]+", "_", s).strip("_")
    return s if s.endswith("isda") else s + "_isda"


def isda_county(base: Path) -> Path | None:
    """County means of iSDAsoil gridded nutrients by zonal statistics over the
    county boundaries. Reads data/raw/isda/*.tif (natural units, 0-30 cm).
    Skips cleanly without geopandas / rasterstats."""
    tifs = sorted((base / "data" / "raw" / "isda").glob("*.tif"))
    if not tifs:
        return None
    try:
        import geopandas as gpd  # type: ignore
        from rasterstats import zonal_stats  # type: ignore
        from .crosswalk import find_admin_layers
    except Exception as exc:  # noqa: BLE001
        print(f"[transform] soil.isda_county skipped: {exc} (install geopandas/rasterstats)")
        return None

    layers = find_admin_layers(base / "data" / "raw")
    adm = layers.get("adm1") or layers.get("adm2")
    if not adm:
        print("[transform] soil.isda_county: no county boundary layer detected under data/raw/cod_ab")
        return None
    counties = (gpd.read_file(adm["path"], layer=adm["layer"]) if adm["layer"]
                else gpd.read_file(adm["path"])).to_crs("EPSG:4326")
    name_col = adm["adm1_name"] or next(
        (c for c in counties.columns if "name" in c.lower() or "county" in c.lower()), None)
    if name_col is None:
        print("[transform] soil.isda_county: county name column not found")
        return None

    out_df = pd.DataFrame({"county_name": counties[name_col].values})
    props = []
    for tif in tifs:
        col = _isda_colname(tif.stem)
        out_df[col] = [s["mean"] for s in zonal_stats(counties, str(tif), stats=["mean"])]
        props.append(col)
    out_df["county_norm"] = out_df["county_name"].map(norm)
    lead = ["county_name", "county_norm"]
    out_df = out_df[lead + [c for c in out_df.columns if c not in lead]]
    out = _out(base, "soil", "isda_county")
    out_df.round(4).to_csv(out, index=False)
    print(f"[transform] soil.isda_county ({len(out_df)} counties, {len(props)} properties "
          f"[{', '.join(props)}]) -> {out.name}")
    return out


def _present(raw: Path, source: str, patterns: list[str]) -> bool:
    d = raw / source
    if not d.exists():
        return False
    return any(next(d.rglob(p), None) is not None for p in patterns)


# --- health: KDHS 2022 recodes -> survey-weighted county estimates ----------
# DHS standard recode variables. Anthropometry z-scores are stored x100 with
# sentinel flags (>= 9990) for missing/implausible; anaemia level is coded
# 1 severe / 2 moderate / 3 mild / 4 not anaemic. These defaults match the
# Kenya children (KR) and women (IR) recodes; override here if a future recode
# renames them.
_KDHS_VARS = {
    "weight": "v005",          # sample weight (divide by 1e6)
    "haz": "hw70", "waz": "hw71", "whz": "hw72",   # height/weight z-scores x100
    "child_anaemia": "hw57",   # child anaemia level (KR)
    "child_anaemia_pr": "hc57",  # child anaemia level (PR household-member recode)
    "weight_hh": "hv005",      # household weight for the PR recode (divide by 1e6)
    "woman_anaemia": "v457",   # woman anaemia level (IR)
    "woman_tested": "v042",    # selected/measured for haemoglobin (IR)
}
_COUNTY_CANDIDATES = ["v024", "shcounty", "scounty", "county", "hv024", "sdistrict", "sdist"]
_Z_VALID = 600  # |z*100| plausible bound; DHS flags use >= 9990


def _kdhs_files(base: Path, subdir: str = "kdhs_2022"):
    """Return the list of DHS Stata recodes under data/external/<subdir>/ or
    data/raw/<subdir>/ (case-insensitive .dta), searching subfolders."""
    found = []
    for parent in ("external", "raw"):
        d = base / "data" / parent / subdir
        if d.exists():
            found += [p for p in d.rglob("*") if p.suffix.lower() == ".dta"]
    return sorted(set(found))


def _pick_county_col(meta, cols: set, county_norms: set) -> str | None:
    """Choose the column whose value labels best match Kenyan county names."""
    labels = getattr(meta, "variable_value_labels", {}) or {}
    best, best_hits = None, 0
    for c in _COUNTY_CANDIDATES:
        if c not in cols:
            continue
        names = {norm(str(v)) for v in labels.get(c, {}).values()}
        hits = len(names & county_norms)
        if hits > best_hits:
            best, best_hits = c, hits
    return best if best_hits >= 20 else None


def _weighted_prevalence(df, flag_col: str, weight_col: str) -> "pd.Series":
    """Per-county weighted prevalence (%) of a 0/1 flag over valid rows. Uses
    Series.groupby to stay clear of the DataFrameGroupBy.apply deprecation and
    to handle boolean / nullable flag columns consistently across pandas."""
    valid = df.loc[df[flag_col].notna(), ["county_norm", weight_col, flag_col]].copy()
    valid[flag_col] = valid[flag_col].astype(float)
    num = (valid[weight_col] * valid[flag_col]).groupby(valid["county_norm"]).sum()
    den = valid[weight_col].groupby(valid["county_norm"]).sum()
    return (100.0 * num / den).rename(flag_col)


def kdhs_county(base: Path, vars: dict | None = None, subdir: str = "kdhs_2022",
                out_name: str = "kdhs_county") -> Path | None:
    """Survey-weighted county estimates from the KDHS 2022 recodes.

    Reads the children's recode (KR) for anthropometry (stunting, wasting,
    underweight) and child anaemia, and the women's recode (IR) for women's
    anaemia, both dropped in data/external/kdhs_2022/. Computes per-county,
    sample-weighted prevalence, joins to the master crosswalk, and writes
    data/processed/health/kdhs_county.csv.

    Returns None with a clear message when the recodes or pyreadstat are not
    available, so the pipeline stays runnable before DHS access is granted.
    This is the county nutrition core for the soil-to-nutrition analysis; the
    four county stunting points from the Action Plan are an external check on it.
    """
    files = _kdhs_files(base, subdir)
    if not files:
        return None
    try:
        import pyreadstat  # noqa: F401
    except ImportError:
        print("[transform] health.kdhs_county: pyreadstat not installed "
              "(pip install pyreadstat) - cannot read DHS .dta recodes")
        return None
    import pyreadstat

    V = {**_KDHS_VARS, **(vars or {})}
    xwalk = _crosswalk(base)
    if xwalk is None:
        print("[transform] health.kdhs_county: crosswalk missing - run the build first")
        return None
    county_norms = set(xwalk["county_norm"]) - {""}

    def load(kind_vars: list[str], extra: tuple = (), prefer: str | None = None,
             weight_var: str | None = None):
        """Find a recode containing the needed vars; read those + any optional
        extras present + weight + county. `prefer` is a recode token (e.g. 'ir',
        'kr', 'pr') used to disambiguate when several recodes carry the same
        variable (v457 appears in IR, BR and KR as the mother's value). The PR
        recode uses the household weight hv005 rather than v005, so `weight_var`
        overrides the default."""
        wv = weight_var or V["weight"]
        ordered = files
        if prefer:
            ordered = sorted(files, key=lambda f: 0 if prefer in str(f).lower() else 1)
        for f in ordered:
            try:
                _, meta = pyreadstat.read_dta(str(f), metadataonly=True)
            except Exception:  # noqa: BLE001
                continue
            cols = set(meta.column_names)
            if wv not in cols or not all(v in cols for v in kind_vars):
                continue
            ccol = _pick_county_col(meta, cols, county_norms)
            if ccol is None:
                print(f"[transform] health.kdhs_county: no county column matched in {f.name} "
                      f"(looked for {_COUNTY_CANDIDATES})")
                continue
            opt = [e for e in extra if e in cols]
            usecols = list(dict.fromkeys([wv, ccol, *kind_vars, *opt]))
            df, meta = pyreadstat.read_dta(str(f), usecols=usecols)
            labels = (meta.variable_value_labels or {}).get(ccol, {})
            df["county_label"] = df[ccol].map(labels).astype("string")
            df["county_norm"] = df["county_label"].map(lambda x: norm(x) if isinstance(x, str) else "")
            df["w"] = df[wv] / 1_000_000.0
            return df, f.name
        return None, None

    # --- children: anthropometry + child anaemia ---------------------------
    kr, kr_name = load([V["haz"], V["waz"], V["whz"]], extra=(V["child_anaemia"],), prefer="kr")
    parts = []
    child_anaemia_done = False
    if kr is not None:
        for z, flag in ((V["haz"], "stunting"), (V["whz"], "wasting"), (V["waz"], "underweight")):
            zc = pd.to_numeric(kr[z], errors="coerce")
            kr[flag] = ((zc.abs() <= _Z_VALID) & (zc < -200)).where(zc.abs() <= _Z_VALID)
        n = kr.groupby("county_norm").size().rename("n_children")
        agg = [n, _weighted_prevalence(kr, "stunting", "w"),
               _weighted_prevalence(kr, "wasting", "w"),
               _weighted_prevalence(kr, "underweight", "w")]
        # child anaemia from KR hw57, but only if the column is actually populated
        if V["child_anaemia"] in kr.columns:
            a = pd.to_numeric(kr[V["child_anaemia"]], errors="coerce")
            kr["child_anaemia"] = a.isin([1, 2, 3]).where(a.isin([1, 2, 3, 4]))
            if kr["child_anaemia"].notna().any():
                agg.append(_weighted_prevalence(kr, "child_anaemia", "w"))
                child_anaemia_done = True
        parts.append(pd.concat(agg, axis=1))
        print(f"[transform] health.kdhs_county: children from {kr_name} "
              f"({int(n.sum())} records)")

    # --- child anaemia fallback: the PR recode (hc57) when KR hw57 is empty --
    # Neither the 2022 nor the 2014 Kenya DHS measured child haemoglobin (anaemia
    # was carried by the Malaria Indicator Surveys instead), so this normally finds
    # nothing here; it stays in place for any round that does measure it.
    if not child_anaemia_done:
        pr, pr_name = load([V["child_anaemia_pr"]], prefer="pr", weight_var=V["weight_hh"])
        if pr is not None:
            a = pd.to_numeric(pr[V["child_anaemia_pr"]], errors="coerce")
            pr = pr.assign(child_anaemia=a.isin([1, 2, 3]).where(a.isin([1, 2, 3, 4])))
            if pr["child_anaemia"].notna().any():
                parts.append(_weighted_prevalence(pr, "child_anaemia", "w").to_frame())
                child_anaemia_done = True
                print(f"[transform] health.kdhs_county: child anaemia from {pr_name} "
                      f"(PR recode {V['child_anaemia_pr']})")

    # --- women: anaemia ----------------------------------------------------
    # Gate on a valid anaemia level (v457 in 1-4), not on v042: when a survey
    # has no haemoglobin module the level is empty and the indicator is omitted.
    ir, ir_name = load([V["woman_anaemia"]], prefer="ir")
    women_anaemia_done = False
    if ir is not None:
        a = pd.to_numeric(ir[V["woman_anaemia"]], errors="coerce")
        wa = a.isin([1, 2, 3]).where(a.isin([1, 2, 3, 4]))
        if wa.notna().any():
            ir = ir.assign(women_anaemia=wa)
            nw = ir.loc[wa.notna()].groupby("county_norm").size().rename("n_women")
            parts.append(pd.concat([nw, _weighted_prevalence(ir, "women_anaemia", "w")], axis=1))
            women_anaemia_done = True
            print(f"[transform] health.kdhs_county: women anaemia from {ir_name} "
                  f"({int(nw.sum())} tested)")

    if not (child_anaemia_done or women_anaemia_done):
        print(f"[transform] health.{out_name}: no haemoglobin/anaemia data present "
              "in these recodes (this round did not include the biomarker module); "
              "reporting stunting, wasting and underweight")

    if not parts:
        print("[transform] health.kdhs_county: recodes present but required variables "
              f"not found (expected {V['haz']}/{V['waz']}/{V['whz']} or {V['woman_anaemia']})")
        return None

    out_df = pd.concat(parts, axis=1).reset_index()
    out_df = out_df[out_df["county_norm"].isin(county_norms)]
    cmap = (xwalk.drop_duplicates("county_norm")
            .set_index("county_norm")[["county_code", "county_name"]])
    out_df = out_df.join(cmap, on="county_norm")
    # Drop any indicator or count column that ended up entirely empty, so the
    # table carries only the outcomes the survey actually measured.
    for c in ("child_anaemia", "women_anaemia", "n_women", "n_children"):
        if c in out_df.columns and out_df[c].notna().sum() == 0:
            out_df = out_df.drop(columns=c)
    pct = [c for c in ("stunting", "wasting", "underweight", "child_anaemia", "women_anaemia")
           if c in out_df.columns]
    out_df[pct] = out_df[pct].round(1)
    lead = [c for c in ("county_code", "county_name") if c in out_df.columns]
    rest = [c for c in out_df.columns if c not in lead + ["county_norm"]]
    out_df = out_df[lead + rest].sort_values("county_name")

    out = _out(base, "health", out_name)
    out_df.to_csv(out, index=False)
    cov = ", ".join(f"{c} {int(out_df[c].notna().sum())}/{len(out_df)}" for c in pct)
    print(f"[transform] health.{out_name} ({len(out_df)} counties; coverage: {cov}) -> {out.name}")
    return out


# --- health: KDHS 2022 child dietary diversity and food-to-body controls -----
# Minimum Dietary Diversity (MDD-IYCF, WHO 2021): a child 6-23 months consumed
# foods from at least five of eight groups in the last 24 hours. The constituent
# DHS children-recode food variables (coded 1 = yes) map to the groups below; a
# group counts if any of its present variables is 1. Also extracts the standard
# food-to-body controls (wealth, maternal education, water and sanitation) that
# the empirical strategy requires for the food-to-body stage.
_MDD_GROUPS = {
    "breastmilk":      ["v404"],                       # currently breastfeeding
    "grains_roots":    ["v414e", "v414f", "v412a"],    # grains; white roots/tubers
    "legumes_nuts":    ["v414o"],                       # beans, peas, lentils, nuts
    "dairy":           ["v411", "v411a", "v414p"],     # milk, formula, cheese/yogurt
    "flesh_foods":     ["v414h", "v414m", "v414n"],    # meat, fish, organ meats
    "eggs":            ["v414g"],                       # eggs
    "vita_fruit_veg":  ["v414i", "v414j", "v414k"],    # vitamin-A rich fruit/veg
    "other_fruit_veg": ["v414l"],                       # other fruit/veg
}
_MDD_FOODVARS = sorted({v for vs in _MDD_GROUPS.values() for v in vs} | {"m4"})
_CONTROL_VARS = ["v190", "v191", "v106", "v133", "v113", "v116", "v025", "h11", "h33"]
_AGE_VARS = ["b19", "hw1", "v008", "b3"]
# Standard DHS / JMP improved classifications (adjustable).
_IMPROVED_WATER = {11, 12, 13, 14, 21, 31, 41, 51, 71, 72, 91, 92}
_IMPROVED_SAN = {11, 12, 13, 14, 15, 21, 22, 41}


def _weighted_mean(df, col: str, weight_col: str) -> "pd.Series":
    """Per-county weighted mean of a numeric column over valid rows."""
    valid = df.loc[df[col].notna(), ["county_norm", weight_col, col]].copy()
    valid[col] = valid[col].astype(float)
    num = (valid[weight_col] * valid[col]).groupby(valid["county_norm"]).sum()
    den = valid[weight_col].groupby(valid["county_norm"]).sum()
    return (num / den).rename(col)


def _child_age_months(df, cols: set):
    """Child age in months from b19, else hw1, else v008 - b3."""
    if "b19" in cols:
        return pd.to_numeric(df["b19"], errors="coerce")
    if "hw1" in cols:
        return pd.to_numeric(df["hw1"], errors="coerce")
    if {"v008", "b3"} <= cols:
        return pd.to_numeric(df["v008"], errors="coerce") - pd.to_numeric(df["b3"], errors="coerce")
    return pd.Series(np.nan, index=df.index)


def _diet_controls_perchild(df, cols: set):
    """Add per-child MDD and control columns; return (df, measurable_groups,
    indicator_names). MDD is set only for children 6-23 months."""
    # --- MDD-IYCF over eight food groups -----------------------------------
    measurable = []
    group_flags = []
    for gname, gvars in _MDD_GROUPS.items():
        present = [v for v in gvars if v in cols]
        if gname == "breastmilk" and "v404" not in cols and "m4" in cols:
            df["_g_breastmilk"] = (pd.to_numeric(df["m4"], errors="coerce") == 95).astype(float)
            group_flags.append("_g_breastmilk")
            measurable.append(gname)
            continue
        if not present:
            continue
        consumed = None
        for v in present:
            yes = (pd.to_numeric(df[v], errors="coerce") == 1)
            consumed = yes if consumed is None else (consumed | yes)
        df[f"_g_{gname}"] = consumed.astype(float)
        group_flags.append(f"_g_{gname}")
        measurable.append(gname)

    indicators = []
    if len(measurable) >= 5:   # only meaningful if most groups are observed
        age = _child_age_months(df, cols)
        ngroups = df[group_flags].sum(axis=1)
        in_window = age.between(6, 23)
        df["mdd"] = ((ngroups >= 5).astype(float)).where(in_window)
        indicators.append("mdd")

    # --- food-to-body controls --------------------------------------------
    if "v191" in cols:
        df["wealth_factor_mean"] = pd.to_numeric(df["v191"], errors="coerce") / 100000.0
        indicators.append("wealth_factor_mean")
    if "v190" in cols:
        q = pd.to_numeric(df["v190"], errors="coerce")
        df["poorest2_share"] = q.isin([1, 2]).astype(float).where(q.notna())
        indicators.append("poorest2_share")
    if "v133" in cols:
        df["edu_years_mean"] = pd.to_numeric(df["v133"], errors="coerce")
        indicators.append("edu_years_mean")
    if "v106" in cols:
        e = pd.to_numeric(df["v106"], errors="coerce")
        df["no_education_share"] = (e == 0).astype(float).where(e.notna() & (e < 8))
        df["secondary_plus_share"] = (e >= 2).astype(float).where(e.notna() & (e < 8))
        indicators += ["no_education_share", "secondary_plus_share"]
    if "v113" in cols:
        w = pd.to_numeric(df["v113"], errors="coerce")
        df["improved_water_share"] = w.isin(_IMPROVED_WATER).astype(float).where(w.notna() & (w < 96))
        indicators.append("improved_water_share")
    if "v116" in cols:
        s = pd.to_numeric(df["v116"], errors="coerce")
        df["improved_sanitation_share"] = s.isin(_IMPROVED_SAN).astype(float).where(s.notna() & (s < 96))
        indicators.append("improved_sanitation_share")
    if "v025" in cols:
        u = pd.to_numeric(df["v025"], errors="coerce")
        df["urban_share"] = (u == 1).astype(float).where(u.notna())
        indicators.append("urban_share")
    # --- disease burden and health-programme controls (children) -----------
    # h11: child had diarrhoea recently (absorption-related control, prompt 6.99);
    # h33: child received a vitamin A supplement (health/programme proxy, 6.97).
    if "h11" in cols:
        di = pd.to_numeric(df["h11"], errors="coerce")
        df["diarrhea_share"] = di.isin([1, 2]).astype(float).where(di.notna() & (di < 8))
        indicators.append("diarrhea_share")
    if "h33" in cols:
        va = pd.to_numeric(df["h33"], errors="coerce")
        df["vit_a_supp_share"] = va.isin([1, 2, 3]).astype(float).where(va.notna() & (va != 8))
        indicators.append("vit_a_supp_share")
    return df, measurable, indicators


def _kdhs_maternal_bmi(base, files, V, county_norms):
    """County maternal BMI (v445/100), thinness (BMI < 18.5) and overweight
    (BMI >= 25) shares from the women's (IR) recode, survey-weighted by v005.
    Returns a frame indexed by county_norm, or None when no IR recode carrying
    v445 is found. Self-contained and called inside a guard, so any failure
    leaves the rest of the controls table intact."""
    try:
        import pyreadstat
    except Exception:  # noqa: BLE001
        return None
    for f in sorted(files, key=lambda x: 0 if "ir" in str(x).lower() else 1):
        try:
            _, meta = pyreadstat.read_dta(str(f), metadataonly=True)
        except Exception:  # noqa: BLE001
            continue
        cols = set(meta.column_names)
        if "v445" not in cols or "v005" not in cols:
            continue
        ccol = _pick_county_col(meta, cols, county_norms)
        if ccol is None:
            continue
        df, meta = pyreadstat.read_dta(str(f), usecols=["v445", "v005", ccol])
        labels = (meta.variable_value_labels or {}).get(ccol, {})
        df["county_norm"] = df[ccol].map(labels).map(lambda x: norm(x) if isinstance(x, str) else "")
        df["w"] = df["v005"] / 1_000_000.0
        bmi = pd.to_numeric(df["v445"], errors="coerce")
        valid = bmi < 9000          # 9996/9998/9999 are DHS missing flags
        df["maternal_bmi"] = (bmi / 100.0).where(valid)
        df["maternal_thinness_share"] = (bmi < 1850).astype(float).where(valid)
        df["maternal_overweight_share"] = (bmi >= 2500).astype(float).where(valid)
        if not df["maternal_bmi"].notna().any():
            return None
        g = pd.concat([
            _weighted_mean(df, "maternal_bmi", "w").rename("maternal_bmi_mean"),
            _weighted_prevalence(df, "maternal_thinness_share", "w"),
            _weighted_prevalence(df, "maternal_overweight_share", "w"),
        ], axis=1)
        print(f"[transform] health.kdhs_controls_county: maternal BMI from {f.name}")
        return g
    return None


def kdhs_diet_controls(base: Path, vars: dict | None = None, subdir: str = "kdhs_2022",
                       out_name: str = "kdhs_controls_county") -> Path | None:
    """Survey-weighted county minimum dietary diversity (MDD-IYCF) and the
    food-to-body controls (wealth, maternal education, water and sanitation)
    from the KDHS 2022 children's recode. Writes health.kdhs_controls_county.

    Skips cleanly (returns None) when the recodes or pyreadstat are absent."""
    files = _kdhs_files(base, subdir)
    if not files:
        return None
    try:
        import pyreadstat
    except ImportError:
        print("[transform] health.kdhs_controls_county: pyreadstat not installed")
        return None

    V = {**_KDHS_VARS, **(vars or {})}
    xwalk = _crosswalk(base)
    if xwalk is None:
        print("[transform] health.kdhs_controls_county: crosswalk missing - run the build first")
        return None
    county_norms = set(xwalk["county_norm"]) - {""}
    want = _MDD_FOODVARS + _CONTROL_VARS + _AGE_VARS

    # Find the children's recode: the one carrying the food-diversity variables.
    ordered = sorted(files, key=lambda f: 0 if "kr" in str(f).lower() else 1)
    kr = kr_name = present = None
    for f in ordered:
        try:
            _, meta = pyreadstat.read_dta(str(f), metadataonly=True)
        except Exception:  # noqa: BLE001
            continue
        cols = set(meta.column_names)
        if V["weight"] not in cols:
            continue
        food_present = [v for v in _MDD_FOODVARS if v in cols]
        if len(food_present) < 6:   # not the diet recode
            continue
        ccol = _pick_county_col(meta, cols, county_norms)
        if ccol is None:
            continue
        usecols = list(dict.fromkeys(
            [V["weight"], ccol, *[v for v in want if v in cols]]))
        df, meta = pyreadstat.read_dta(str(f), usecols=usecols)
        labels = (meta.variable_value_labels or {}).get(ccol, {})
        df["county_norm"] = df[ccol].map(labels).map(lambda x: norm(x) if isinstance(x, str) else "")
        df["w"] = df[V["weight"]] / 1_000_000.0
        kr, kr_name, present = df, f.name, cols
        break

    if kr is None:
        print("[transform] health.kdhs_controls_county: no recode with the dietary "
              "variables found (expected the children's KR recode)")
        return None

    kr, measurable, indicators = _diet_controls_perchild(kr, present)
    if not indicators:
        print("[transform] health.kdhs_controls_county: no MDD or control variables present")
        return None

    parts = []
    if "mdd" in indicators:
        n6 = (kr.loc[kr["mdd"].notna()].groupby("county_norm").size().rename("n_children_6_23"))
        parts += [n6, _weighted_prevalence(kr, "mdd", "w")]
    share_pct = ["poorest2_share", "no_education_share", "secondary_plus_share",
                 "improved_water_share", "improved_sanitation_share", "urban_share",
                 "diarrhea_share", "vit_a_supp_share"]
    for ind in indicators:
        if ind == "mdd":
            continue
        if ind in share_pct:
            parts.append(_weighted_prevalence(kr, ind, "w"))
        else:
            parts.append(_weighted_mean(kr, ind, "w"))

    out_df = pd.concat(parts, axis=1).reset_index()
    out_df = out_df[out_df["county_norm"].isin(county_norms)]
    # maternal BMI from the women's recode (body-nutrient vector); guarded so a
    # failure here never drops the dietary-diversity and control columns above.
    try:
        bmi = _kdhs_maternal_bmi(base, files, V, county_norms)
        if bmi is not None and not bmi.empty:
            out_df = out_df.merge(bmi.reset_index(), on="county_norm", how="left")
    except Exception as exc:  # noqa: BLE001
        print(f"[transform] health.kdhs_controls_county: maternal BMI skipped: {exc}")
    cmap = (xwalk.drop_duplicates("county_norm")
            .set_index("county_norm")[["county_code", "county_name"]])
    out_df = out_df.join(cmap, on="county_norm")
    round1 = [c for c in ("mdd", *share_pct) if c in out_df.columns]
    out_df[round1] = out_df[round1].round(1)
    for c in ("wealth_factor_mean", "edu_years_mean"):
        if c in out_df.columns:
            out_df[c] = out_df[c].round(3)
    for c in ("maternal_bmi_mean", "maternal_thinness_share", "maternal_overweight_share"):
        if c in out_df.columns:
            out_df[c] = out_df[c].round(1)
    lead = [c for c in ("county_code", "county_name") if c in out_df.columns]
    rest = [c for c in out_df.columns if c not in lead + ["county_norm"]]
    out_df = out_df[lead + rest].sort_values("county_name")

    out = _out(base, "health", out_name)
    out_df.to_csv(out, index=False)
    print(f"[transform] health.{out_name} ({len(out_df)} counties; "
          f"MDD groups measurable {len(measurable)}/8; indicators: "
          f"{', '.join(indicators)}) -> {out.name}")
    return out


def run_all(base: Path) -> None:
    """Run every transform and report, per layer, whether its input was found.

    A transform that finds no input prints exactly which folder and file
    pattern it looked for, so a --build-only run tells you what still needs to
    be downloaded or unzipped rather than failing silently.
    """
    print("[transform] running normalisation transforms")
    raw = base / "data" / "raw"

    checks = [
        ("health.wb_hnp_panel", "wb_hnp", ["*.json"], wb_hnp_panel),
        ("food.faostat_kenya", "faostat", ["*_normalized.zip"], faostat_kenya),
        ("soil.soilgrids_zonal_county", "soilgrids", ["*.tif"], soilgrids_zonal),
        ("soil.afsis_county", "afsis_chem", ["*.csv"], afsis_county_chem),
        ("soil.isda_county", "isda", ["*.tif"], isda_county),
    ]
    for label, source, patterns, fn in checks:
        if _present(raw, source, patterns):
            try:
                if fn(base) is None:
                    print(f"[transform] {label}: input present but produced no rows "
                          f"(check column names / contents in data/raw/{source})")
            except Exception as exc:  # noqa: BLE001
                print(f"[transform] {label}: error {type(exc).__name__}: {exc}")
        else:
            print(f"[transform] {label}: SKIP - no input at "
                  f"data/raw/{source}/ matching {patterns}")

    # prices read two sources independently, from raw OR the manual external drop
    def _has(source: str) -> bool:
        for parent in ("raw", "external"):
            d = base / "data" / parent / source
            if d.exists() and (next(d.rglob("*.csv"), None) or next(d.rglob("*.xlsx"), None)):
                return True
        return False

    wfp_ok, wb_ok = _has("wfp_prices"), _has("wb_rtfp")
    if wfp_ok or wb_ok:
        prices(base)
    if not wfp_ok:
        print("[transform] food.prices_wfp_observed: SKIP - no file in "
              "data/raw/wfp_prices/ (run: python run_all.py --layer food)")
    if not wb_ok:
        print("[transform] food.prices_wb_modeled: SKIP - no file in data/external/wb_rtfp/ "
              "(manual download - see MANUAL_DATASETS.md)")

    # KDHS 2022 county anthropometry + anaemia (dedicated survey transform;
    # microdata is .dta, which the generic ingester deliberately skips).
    if _kdhs_files(base):
        try:
            if kdhs_county(base) is None:
                print("[transform] health.kdhs_county: recodes present but produced no "
                      "rows (check recode variables / pyreadstat)")
        except Exception as exc:  # noqa: BLE001
            print(f"[transform] health.kdhs_county: error {type(exc).__name__}: {exc}")
        try:
            if kdhs_diet_controls(base) is None:
                print("[transform] health.kdhs_controls_county: recodes present but "
                      "produced no rows (check dietary / control variables)")
        except Exception as exc:  # noqa: BLE001
            print(f"[transform] health.kdhs_controls_county: error {type(exc).__name__}: {exc}")
    # KDHS 2014 (phase 72): a second time point. The 2014 biomarker module
    # carries haemoglobin, so this round also yields child and women anaemia,
    # which the 2022 round omits, and gives a 2014-to-2022 trend for stunting.
    if _kdhs_files(base, "kdhs_2014"):
        try:
            kdhs_county(base, subdir="kdhs_2014", out_name="kdhs_county_2014")
        except Exception as exc:  # noqa: BLE001
            print(f"[transform] health.kdhs_county_2014: error {type(exc).__name__}: {exc}")
        try:
            kdhs_diet_controls(base, subdir="kdhs_2014", out_name="kdhs_controls_county_2014")
        except Exception as exc:  # noqa: BLE001
            print(f"[transform] health.kdhs_controls_county_2014: error {type(exc).__name__}: {exc}")
    else:
        print("[transform] health.kdhs_county: SKIP - no .dta recodes in "
              "data/external/kdhs_2022/ (manual gate - see MANUAL_DATASETS.md)")

    # generic ingestion of any other manual / gated drops
    ingest_external(base)

    print("[transform] done")
