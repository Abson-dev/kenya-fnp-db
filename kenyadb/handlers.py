"""Acquisition handlers.

Each handler has the signature handler(meta, ctx) and is responsible for
fetching its source and recording one provenance entry per output file (or
one entry describing a manual gate). The orchestrator in pipeline.py maps
the registry `handler` field to the functions in HANDLERS at the bottom.

Network-dependent handlers degrade gracefully: on any error they record a
`failed` provenance row instead of crashing the run, so a single dead
endpoint never takes down the whole pipeline.

Author: Aboubacar HEMA
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .utils import io
from .utils.provenance import Provenance


@dataclass
class Ctx:
    layer: str
    source_key: str
    raw_dir: Path
    external_dir: Path
    prov: Provenance
    dry_run: bool = False


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


def _record_file(ctx: Ctx, meta: dict, path: Path, message: str = "") -> None:
    ctx.prov.record(
        layer=ctx.layer,
        source_key=ctx.source_key,
        meta=meta,
        local_path=str(path),
        sha256=io.sha256(path),
        nbytes=path.stat().st_size,
        status="ok",
        message=message,
    )


def _record_status(ctx: Ctx, meta: dict, status: str, message: str) -> None:
    ctx.prov.record(
        layer=ctx.layer, source_key=ctx.source_key, meta=meta,
        status=status, message=message,
    )


# ---------------------------------------------------------------------------
# generic handlers
# ---------------------------------------------------------------------------
def http_file(meta: dict, ctx: Ctx) -> None:
    """Download a single direct file. Honours an optional `datahub` key as a
    preferred bulk URL (used by the World Bank real-time prices source)."""
    url = meta.get("datahub") or meta["url"]
    out = ctx.raw_dir / ctx.source_key / _safe(Path(urlparse(url).path).name or f"{ctx.source_key}.bin")
    if ctx.dry_run:
        _record_status(ctx, meta, "skipped", f"dry-run: would GET {url}")
        return
    try:
        path = io.http_download(url, out)
        _record_file(ctx, meta, path)
    except Exception as exc:  # noqa: BLE001
        _record_status(ctx, meta, "failed", f"{type(exc).__name__}: {exc}")


def hdx_dataset(meta: dict, ctx: Ctx) -> None:
    """Resolve an HDX (CKAN) dataset and download every resource."""
    ds = meta["hdx_dataset"]
    api = f"https://data.humdata.org/api/3/action/package_show?id={ds}"
    if ctx.dry_run:
        _record_status(ctx, meta, "skipped", f"dry-run: would query HDX {ds}")
        return
    try:
        payload = io.get_json(api)
        resources = payload["result"]["resources"]
    except Exception as exc:  # noqa: BLE001
        _record_status(ctx, meta, "failed", f"package_show: {exc}")
        return
    got = 0
    for res in resources:
        rurl = res.get("download_url") or res.get("url")
        if not rurl:
            continue
        name = _safe(res.get("name") or Path(urlparse(rurl).path).name)
        out = ctx.raw_dir / ctx.source_key / name
        try:
            path = io.http_download(rurl, out)
            _record_file(ctx, meta, path, message=f"resource {res.get('format', '')}")
            got += 1
        except Exception as exc:  # noqa: BLE001
            _record_status(ctx, meta, "failed", f"resource {name}: {exc}")
    if got == 0:
        _record_status(ctx, meta, "failed", "no resources downloaded")


def ckan_resource(meta: dict, ctx: Ctx) -> None:
    """Generic CKAN dataset on an arbitrary domain (e.g. SODMA mirror)."""
    parsed = urlparse(meta["url"])
    slug = parsed.path.rstrip("/").split("/")[-1]
    api = f"{parsed.scheme}://{parsed.netloc}/api/3/action/package_show?id={slug}"
    if ctx.dry_run:
        _record_status(ctx, meta, "skipped", f"dry-run: would query CKAN {slug}")
        return
    try:
        payload = io.get_json(api)
        resources = payload["result"]["resources"]
    except Exception as exc:  # noqa: BLE001
        _record_status(ctx, meta, "failed", f"package_show: {exc}")
        return
    for res in resources:
        rurl = res.get("url")
        if not rurl:
            continue
        name = _safe(res.get("name") or Path(urlparse(rurl).path).name)
        out = ctx.raw_dir / ctx.source_key / name
        try:
            path = io.http_download(rurl, out)
            _record_file(ctx, meta, path, message=f"resource {res.get('format', '')}")
        except Exception as exc:  # noqa: BLE001
            _record_status(ctx, meta, "failed", f"resource {name}: {exc}")


# ---------------------------------------------------------------------------
# soil
# ---------------------------------------------------------------------------
# Kenya bounding box (W, S, E, N) in EPSG:4326
KENYA_BBOX = (33.9, -4.7, 41.9, 5.5)


def soilgrids_wcs(meta: dict, ctx: Ctx) -> None:
    """Fetch SoilGrids coverages for Kenya via WCS GetCoverage, one GeoTIFF
    per property x depth. SoilGrids serves each property on its own WCS map
    (https://maps.isric.org/mapserv?map=/map/<property>.map). Downstream
    zonal statistics are computed against the COD-AB polygons in transforms.
    """
    base = meta["base_url"]
    props = meta["properties"]
    depths = meta["depths"]
    w, s, e, n = KENYA_BBOX
    if ctx.dry_run:
        _record_status(ctx, meta, "skipped",
                       f"dry-run: {len(props)}x{len(depths)} WCS coverages over {KENYA_BBOX}")
        return
    out_dir = ctx.raw_dir / ctx.source_key
    got = 0
    for prop in props:
        map_q = f"/map/{prop}.map"
        for depth in depths:
            cov = f"{prop}_{depth}_mean"
            params = {
                "map": map_q,
                "SERVICE": "WCS",
                "VERSION": "2.0.1",
                "REQUEST": "GetCoverage",
                "COVERAGEID": cov,
                "FORMAT": "image/tiff",
                "SUBSET": [f"X({w},{e})", f"Y({s},{n})"],
                "SUBSETTINGCRS": "http://www.opengis.net/def/crs/EPSG/0/4326",
                "OUTPUTCRS": "http://www.opengis.net/def/crs/EPSG/0/4326",
            }
            out = out_dir / f"{cov}.tif"
            try:
                path = io.http_download(base, out, params=params)
                if path.stat().st_size > 1024:  # skip empty/ error tiffs
                    _record_file(ctx, meta, path, message=f"{prop} {depth}")
                    got += 1
            except Exception as exc:  # noqa: BLE001
                _record_status(ctx, meta, "failed", f"{cov}: {exc}")
    if got == 0:
        _record_status(ctx, meta, "failed", "no SoilGrids coverages retrieved")


def aws_s3(meta: dict, ctx: Ctx) -> None:
    """List and download an anonymous (public) S3 bucket over HTTPS, no
    credentials and no boto3. Used for the AfSIS soil-chemistry bucket."""
    bucket = meta["bucket"]
    prefix = meta.get("prefix", "")
    endpoint = f"https://{bucket}.s3.amazonaws.com/"
    if ctx.dry_run:
        _record_status(ctx, meta, "skipped", f"dry-run: would list s3://{bucket}/{prefix}")
        return
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    keys: list[str] = []
    token = None
    try:
        while True:
            params = {"list-type": "2", "prefix": prefix}
            if token:
                params["continuation-token"] = token
            import requests
            r = requests.get(endpoint, params=params, timeout=120,
                             headers={"User-Agent": io.USER_AGENT})
            r.raise_for_status()
            root = ET.fromstring(r.text)
            for c in root.findall("s3:Contents", ns):
                k = c.findtext("s3:Key", default="", namespaces=ns)
                if k and not k.endswith("/"):
                    keys.append(k)
            trunc = root.findtext("s3:IsTruncated", default="false", namespaces=ns)
            if trunc.lower() == "true":
                token = root.findtext("s3:NextContinuationToken", namespaces=ns)
            else:
                break
    except Exception as exc:  # noqa: BLE001
        _record_status(ctx, meta, "failed", f"s3 list: {exc}")
        return
    # AfSIS is large; fetch reference-chemistry CSV/manifest objects only by default.
    wanted = [k for k in keys if k.lower().endswith((".csv", ".json", ".txt", ".md"))]
    targets = wanted or keys[:5]
    for k in targets:
        out = ctx.raw_dir / ctx.source_key / _safe(k)
        try:
            path = io.http_download(endpoint + k, out)
            _record_file(ctx, meta, path, message=f"s3 key {k}")
        except Exception as exc:  # noqa: BLE001
            _record_status(ctx, meta, "failed", f"s3 get {k}: {exc}")
    _record_status(ctx, meta, "ok",
                   f"listed {len(keys)} keys; downloaded {len(targets)} tabular objects. "
                   "Bulk spectra left in-bucket; pull on demand.")


# ---------------------------------------------------------------------------
# food
# ---------------------------------------------------------------------------
def faostat_bulk(meta: dict, ctx: Ctx) -> None:
    """Download FAOSTAT normalised bulk zips per domain. Kenya (area 114) is
    filtered out of the normalised CSV during the transform step."""
    base = meta["base_url"]
    if ctx.dry_run:
        _record_status(ctx, meta, "skipped",
                       f"dry-run: would fetch {len(meta['domains'])} FAOSTAT domains")
        return
    for dom in meta["domains"]:
        url = f"{base}/{dom}_E_All_Data_(Normalized).zip"
        out = ctx.raw_dir / ctx.source_key / f"{dom}_normalized.zip"
        try:
            path = io.http_download(url, out)
            _record_file(ctx, meta, path, message=f"FAOSTAT domain {dom}")
        except Exception as exc:  # noqa: BLE001
            _record_status(ctx, meta, "failed", f"domain {dom}: {exc}")


def kilimostat_api(meta: dict, ctx: Ctx) -> None:
    """Pull KilimoSTAT open data. The platform exposes CSV exports and an API;
    endpoint structure is captured here defensively. If the export endpoint
    is unreachable, the source is flagged for manual export from the portal."""
    if ctx.dry_run:
        _record_status(ctx, meta, "skipped", "dry-run: would query KilimoSTAT API")
        return
    candidates = [
        "https://statistics.kilimo.go.ke/api/datasets?format=json",
        "https://statistics.kilimo.go.ke/en/api/v1/datasets/",
    ]
    for url in candidates:
        try:
            payload = io.get_json(url)
            out = ctx.raw_dir / ctx.source_key / "kilimostat_catalog.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            import json
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            _record_file(ctx, meta, out, message=f"catalog via {url}")
            return
        except Exception:  # noqa: BLE001
            continue
    _record_status(ctx, meta, "manual",
                   "KilimoSTAT API not reachable from pipeline; export crop/livestock/"
                   "price CSVs from https://statistics.kilimo.go.ke into data/external/kilimostat/")


def worldbank_api(meta: dict, ctx: Ctx) -> None:
    """Pull World Bank HNP indicators for Kenya via the public v2 API."""
    if ctx.dry_run:
        _record_status(ctx, meta, "skipped",
                       f"dry-run: would fetch {len(meta['indicators'])} WB indicators")
        return
    country = meta["country"]
    import json
    out_dir = ctx.raw_dir / ctx.source_key
    got = 0
    for ind in meta["indicators"]:
        url = f"https://api.worldbank.org/v2/country/{country}/indicator/{ind}"
        try:
            payload = io.get_json(url, params={"format": "json", "per_page": 20000})
            out = out_dir / f"{ind}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            _record_file(ctx, meta, out, message=f"indicator {ind}")
            got += 1
        except Exception as exc:  # noqa: BLE001
            _record_status(ctx, meta, "failed", f"indicator {ind}: {exc}")
    if got == 0:
        _record_status(ctx, meta, "failed", "no WB indicators retrieved")


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------
def faolex_api(meta: dict, ctx: Ctx) -> None:
    """FAOLEX has no clean bulk API; capture the Kenya results landing for
    document discovery and flag full-text harvesting as a manual review step."""
    if ctx.dry_run:
        _record_status(ctx, meta, "skipped", "dry-run: FAOLEX Kenya landing")
        return
    _record_status(ctx, meta, "manual",
                   "FAOLEX lacks a bulk API. Use the Kenya country profile to export the "
                   "food/nutrition, land/soil, water, environment and social-protection "
                   "record lists, then store abstracts + PDFs in data/external/faolex/.")


def gfdx_download(meta: dict, ctx: Ctx) -> None:
    """GFDx publishes country data exports. Try the standard export endpoints;
    fall back to a manual flag if the export schema has changed."""
    if ctx.dry_run:
        _record_status(ctx, meta, "skipped", "dry-run: GFDx Kenya export")
        return
    candidates = [
        "https://www.fortificationdata.org/wp-content/themes/gfdx/api/export.php?country=KEN",
        "https://www.fortificationdata.org/api/v1/country/KEN/export?format=csv",
    ]
    for url in candidates:
        out = ctx.raw_dir / ctx.source_key / "gfdx_kenya.csv"
        try:
            path = io.http_download(url, out)
            if path.stat().st_size > 200:
                _record_file(ctx, meta, path, message=f"GFDx export via {url}")
                return
        except Exception:  # noqa: BLE001
            continue
    _record_status(ctx, meta, "manual",
                   "Export the Kenya dashboard (maize flour, oil, rice, salt, wheat flour) "
                   "from https://www.fortificationdata.org into data/external/gfdx/.")


# ---------------------------------------------------------------------------
# health - registration / agreement gates
# ---------------------------------------------------------------------------
def dhs_rdhs(meta: dict, ctx: Ctx) -> None:
    """KDHS requires a free DHS account and per-dataset approval, so it cannot
    be fully automated. Records a manual gate with the rdhs recipe."""
    _record_status(
        ctx, meta, "manual",
        "Register at dhsprogram.com, request the Kenya 2022 datasets, then in R: "
        "library(rdhs); set_rdhs_config(email=..., project=...); "
        "get_datasets(c('KEHR8BFL','KEPR8BFL','KEKR8BFL','KEIR8BFL','KEMR8BFL')). "
        "Place the extracted .DTA/.SAV files in data/external/kdhs_2022/.",
    )


def manual(meta: dict, ctx: Ctx) -> None:
    """Generic manual gate (dashboards, DUA-only microdata, strategy PDFs that
    sit behind portals). Records the note from the registry so MANUAL_DATASETS
    and the provenance table both carry the instruction."""
    note = meta.get("note") or f"Manual acquisition required from {meta.get('url', 'source')}."
    _record_status(ctx, meta, "manual", note)


HANDLERS = {
    "http_file": http_file,
    "hdx_dataset": hdx_dataset,
    "ckan_resource": ckan_resource,
    "soilgrids_wcs": soilgrids_wcs,
    "aws_s3": aws_s3,
    "faostat_bulk": faostat_bulk,
    "kilimostat_api": kilimostat_api,
    "worldbank_api": worldbank_api,
    "faolex_api": faolex_api,
    "gfdx_download": gfdx_download,
    "dhs_rdhs": dhs_rdhs,
    "manual": manual,
}
