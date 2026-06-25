"""Download Kenya CHIRPS rainfall and MODIS NDVI annual rasters through Google
Earth Engine, straight into the pipeline input folders. This is the recommended
route for both products: it returns clean, Kenya-clipped annual GeoTIFFs named
with the year, which is exactly what kenyadb.remote_sensing expects.

Run on a machine where Earth Engine is authenticated:

    pip install earthengine-api requests
    earthengine authenticate                      # one time, opens a browser
    python tools/download_remote_sensing_gee.py --project ee-aboubacarhema94 --start 2010 --end 2024

Produces:
    data/raw/chirps_rainfall/chirps_YYYY.tif      annual rainfall total (mm)
    data/raw/modis_ndvi/ndvi_YYYY.tif             annual mean NDVI (MODIS scaled)

Then rebuild the layer:
    python run_all.py --layer geography
    python analyze.py

Notes
-----
- Each export is a small Kenya-clipped raster fetched with getDownloadURL, so no
  Drive export or batch task is needed.
- MODIS NDVI is delivered as scaled integers; kenyadb.remote_sensing rescales it
  to the -1..1 range automatically.
"""
from __future__ import annotations

import argparse
from pathlib import Path

# Kenya bounding box (lon/lat), padded a little beyond the borders.
KENYA_BBOX = [33.8, -4.8, 42.0, 5.6]
CHIRPS_SCALE_M = 5566   # CHIRPS native resolution is about 0.05 degrees
NDVI_SCALE_M = 1000     # MOD13A2 is 1 km


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", type=int, default=2010, help="first year (inclusive)")
    ap.add_argument("--end", type=int, default=2024, help="last year (inclusive)")
    ap.add_argument("--project", default=None, help="your Earth Engine Cloud project id")
    ap.add_argument("--base", default=".", help="path to the kenya_fnp_db root")
    ap.add_argument("--skip-chirps", action="store_true")
    ap.add_argument("--skip-ndvi", action="store_true")
    args = ap.parse_args()

    import ee
    import requests

    if args.project:
        ee.Initialize(project=args.project)
    else:
        ee.Initialize()

    region = ee.Geometry.Rectangle(KENYA_BBOX)
    base = Path(args.base)
    chirps_dir = base / "data" / "raw" / "chirps_rainfall"
    ndvi_dir = base / "data" / "raw" / "modis_ndvi"
    chirps_dir.mkdir(parents=True, exist_ok=True)
    ndvi_dir.mkdir(parents=True, exist_ok=True)

    def fetch(img, scale: int, out: Path) -> None:
        url = img.getDownloadURL({"region": region, "scale": scale,
                                  "crs": "EPSG:4326", "format": "GEO_TIFF"})
        r = requests.get(url, timeout=600)
        r.raise_for_status()
        out.write_bytes(r.content)
        print(f"  saved {out.name} ({len(r.content) // 1024} KB)")

    for y in range(args.start, args.end + 1):
        d0, d1 = f"{y}-01-01", f"{y + 1}-01-01"
        if not args.skip_chirps:
            print(f"[{y}] CHIRPS annual rainfall")
            rain = (ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
                    .filterDate(d0, d1).sum().clip(region))
            try:
                fetch(rain, CHIRPS_SCALE_M, chirps_dir / f"chirps_{y}.tif")
            except Exception as exc:  # noqa: BLE001
                print(f"  CHIRPS {y} failed: {exc}")
        if not args.skip_ndvi:
            print(f"[{y}] MODIS annual mean NDVI")
            ndvi = (ee.ImageCollection("MODIS/061/MOD13A2")
                    .filterDate(d0, d1).select("NDVI").mean().clip(region))
            try:
                fetch(ndvi, NDVI_SCALE_M, ndvi_dir / f"ndvi_{y}.tif")
            except Exception as exc:  # noqa: BLE001
                print(f"  NDVI {y} failed: {exc}")

    print("\nDone. Next: python run_all.py --layer geography   then   python analyze.py")


if __name__ == "__main__":
    main()
