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
    "woman_anaemia": "v457",   # woman anaemia level (IR)
    "woman_tested": "v042",    # selected/measured for haemoglobin (IR)
}
_COUNTY_CANDIDATES = ["v024", "shcounty", "scounty", "county", "hv024", "sdistrict", "sdist"]
_Z_VALID = 600  # |z*100| plausible bound; DHS flags use >= 9990


def _kdhs_files(base: Path):
    """Return the list of DHS Stata recodes under data/external/kdhs_2022/ or
    data/raw/kdhs_2022/ (case-insensitive .dta)."""
    found = []
    for parent in ("external", "raw"):
        d = base / "data" / parent / "kdhs_2022"
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
    """Per-county weighted prevalence (%) of a 0/1 flag over valid rows."""
    valid = df[df[flag_col].notna()]
    num = valid.assign(_w=valid[weight_col] * valid[flag_col]).groupby("county_norm")["_w"].sum()
    den = valid.groupby("county_norm").apply(lambda g: (g[weight_col]).sum())
    return (100.0 * num / den).rename(flag_col)


def kdhs_county(base: Path, vars: dict | None = None) -> Path | None:
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
    files = _kdhs_files(base)
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

    def load(kind_vars: list[str], extra: tuple = ()):
        """Find a recode containing the needed vars; read those + any optional
        extras present + weight + county."""
        for f in files:
            try:
                _, meta = pyreadstat.read_dta(str(f), metadataonly=True)
            except Exception:  # noqa: BLE001
                continue
            cols = set(meta.column_names)
            if not all(v in cols for v in kind_vars):
                continue
            ccol = _pick_county_col(meta, cols, county_norms)
            if ccol is None:
                print(f"[transform] health.kdhs_county: no county column matched in {f.name} "
                      f"(looked for {_COUNTY_CANDIDATES})")
                continue
            opt = [e for e in extra if e in cols]
            usecols = list(dict.fromkeys([V["weight"], ccol, *kind_vars, *opt]))
            df, meta = pyreadstat.read_dta(str(f), usecols=usecols)
            labels = (meta.variable_value_labels or {}).get(ccol, {})
            df["county_label"] = df[ccol].map(labels).astype("string")
            df["county_norm"] = df["county_label"].map(lambda x: norm(x) if isinstance(x, str) else "")
            df["w"] = df[V["weight"]] / 1_000_000.0
            return df, f.name
        return None, None

    # --- children: anthropometry + child anaemia ---------------------------
    kr, kr_name = load([V["haz"], V["waz"], V["whz"]], extra=(V["child_anaemia"],))
    parts = []
    if kr is not None:
        for z, flag in ((V["haz"], "stunting"), (V["whz"], "wasting"), (V["waz"], "underweight")):
            kr[flag] = ((kr[z].abs() <= _Z_VALID) & (kr[z] < -200)).where(kr[z].abs() <= _Z_VALID)
        if V["child_anaemia"] in kr.columns:
            a = kr[V["child_anaemia"]]
            kr["child_anaemia"] = a.isin([1, 2, 3]).where(a.isin([1, 2, 3, 4]))
        n = kr.groupby("county_norm").size().rename("n_children")
        agg = [n]
        for flag in ("stunting", "wasting", "underweight",
                     *( ["child_anaemia"] if "child_anaemia" in kr.columns else [] )):
            agg.append(_weighted_prevalence(kr, flag, "w"))
        parts.append(pd.concat(agg, axis=1))
        print(f"[transform] health.kdhs_county: children from {kr_name} "
              f"({int(n.sum())} records)")

    # --- women: anaemia ----------------------------------------------------
    ir, ir_name = load([V["woman_anaemia"]], extra=(V["woman_tested"],))
    if ir is not None:
        if V["woman_tested"] in ir.columns:
            ir = ir[ir[V["woman_tested"]] == 1]
        a = ir[V["woman_anaemia"]]
        ir["women_anaemia"] = a.isin([1, 2, 3]).where(a.isin([1, 2, 3, 4]))
        nw = ir.groupby("county_norm").size().rename("n_women")
        parts.append(pd.concat([nw, _weighted_prevalence(ir, "women_anaemia", "w")], axis=1))
        print(f"[transform] health.kdhs_county: women from {ir_name} ({int(nw.sum())} records)")

    if not parts:
        print("[transform] health.kdhs_county: recodes present but required variables "
              f"not found (expected {V['haz']}/{V['waz']}/{V['whz']} or {V['woman_anaemia']})")
        return None

    out_df = pd.concat(parts, axis=1).reset_index()
    out_df = out_df[out_df["county_norm"].isin(county_norms)]
    cmap = (xwalk.drop_duplicates("county_norm")
            .set_index("county_norm")[["county_code", "county_name"]])
    out_df = out_df.join(cmap, on="county_norm")
    pct = [c for c in ("stunting", "wasting", "underweight", "child_anaemia", "women_anaemia")
           if c in out_df.columns]
    out_df[pct] = out_df[pct].round(1)
    lead = [c for c in ("county_code", "county_name") if c in out_df.columns]
    rest = [c for c in out_df.columns if c not in lead + ["county_norm"]]
    out_df = out_df[lead + rest].sort_values("county_name")

    out = _out(base, "health", "kdhs_county")
    out_df.to_csv(out, index=False)
    print(f"[transform] health.kdhs_county ({len(out_df)} counties, "
          f"indicators: {', '.join(pct)}) -> {out.name}")
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
    else:
        print("[transform] health.kdhs_county: SKIP - no .dta recodes in "
              "data/external/kdhs_2022/ (manual gate - see MANUAL_DATASETS.md)")

    # generic ingestion of any other manual / gated drops
    ingest_external(base)

    print("[transform] done")
