"""Download Kenya soil moisture (SMAP) and erosion-risk proxy layers through
Google Earth Engine, as Kenya-clipped summary GeoTIFFs that the pipeline picks
up automatically.

The rasters are written to data/external/remote_sensing/, which
kenyadb.remote_sensing zonal-averages into county columns of
geography.remote_sensing_county with no further configuration. This closes the
two soil-vector gaps the framework names (soil moisture and erosion risk).

Run on a machine where Earth Engine is authenticated:

    pip install earthengine-api requests
    earthengine authenticate                      # one time, opens a browser
    python tools/download_soil_moisture_erosion_gee.py --project YOUR_EE_PROJECT

Produces (data/external/remote_sensing/):
    soil_moisture_surface.tif     SMAP mean surface soil moisture (m3/m3)
    soil_moisture_rootzone.tif    SMAP mean root-zone soil moisture (m3/m3)
    erosion_rain_r.tif            RUSLE R factor (rainfall erosivity) from CHIRPS
    slope_deg.tif                 mean slope (degrees) from SRTM
    erosion_risk_index.tif        relative erosion risk = R x S (RUSLE proxy)

Then rebuild the layer:
    python run_all.py --layer geography
    python analyze.py

Notes
-----
- These erosion layers are RUSLE-FACTOR PROXIES, not measured soil loss: R is the
  rainfall-erosivity factor from mean annual rainfall (Renard and Freimund), S is
  the RUSLE slope-steepness factor, and the index is their product. They give a
  defensible relative erosion-risk gradient between counties, not an absolute
  tonnes-per-hectare figure; document them as such.
- SMAP L4 (SPL4SMGP/008) soil moisture is available from 2015 onward; the
  default averages per-year composites across that period.
- Each export is a small Kenya-clipped raster fetched with getDownloadURL, so no
  Drive export or batch task is needed.
"""
from __future__ import annotations

import argparse
from pathlib import Path

# Kenya bounding box (lon/lat), padded a little beyond the borders.
KENYA_BBOX = [33.8, -4.8, 42.0, 5.6]
SM_SCALE_M = 11000      # SMAP L4 native resolution is about 11 km
TERRAIN_SCALE_M = 5000  # coarse is fine for county zonal means and keeps files small


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", default=None, help="your Earth Engine Cloud project id")
    ap.add_argument("--base", default=".", help="path to the kenya_fnp_db root")
    ap.add_argument("--rain-start", default="2010",
                    help="first year for the R factor and soil moisture (clamped to 2015 for SMAP)")
    ap.add_argument("--rain-end", default="2024",
                    help="last year for the R factor and soil moisture")
    ap.add_argument("--skip-soil-moisture", action="store_true")
    ap.add_argument("--skip-erosion", action="store_true")
    args = ap.parse_args()

    import ee
    import requests

    if args.project:
        ee.Initialize(project=args.project)
    else:
        ee.Initialize()

    region = ee.Geometry.Rectangle(KENYA_BBOX)
    out_dir = Path(args.base) / "data" / "external" / "remote_sensing"
    out_dir.mkdir(parents=True, exist_ok=True)

    def fetch(img, scale: int, out: Path) -> None:
        url = img.getDownloadURL({"region": region, "scale": scale,
                                  "crs": "EPSG:4326", "format": "GEO_TIFF"})
        r = requests.get(url, timeout=600)
        r.raise_for_status()
        out.write_bytes(r.content)
        print(f"  saved {out.name} ({len(r.content) // 1024} KB)")

    if not args.skip_soil_moisture:
        print("[soil moisture] SMAP L4 mean surface and root-zone")
        # Average per-year composites first, then average those, so the final
        # reducer sees about a dozen images rather than tens of thousands of
        # 3-hourly frames (which exceeds the getDownloadURL memory limit and
        # returns a 400). SMAP L4 begins in 2015, so the start year is clamped.
        sm_y0 = max(2015, int(args.rain_start))
        sm_y1 = int(args.rain_end)
        sm_years = ee.List.sequence(sm_y0, sm_y1)
        smap = ee.ImageCollection("NASA/SMAP/SPL4SMGP/008")

        def annual_sm(y):
            y = ee.Number(y)
            return (smap.filterDate(ee.Date.fromYMD(y, 1, 1),
                                    ee.Date.fromYMD(y.add(1), 1, 1))
                    .select(["sm_surface", "sm_rootzone"]).mean())

        sm_mean = ee.ImageCollection(sm_years.map(annual_sm)).mean().clip(region)
        surf = sm_mean.select("sm_surface")
        root = sm_mean.select("sm_rootzone")
        try:
            fetch(surf, SM_SCALE_M, out_dir / "soil_moisture_surface.tif")
            fetch(root, SM_SCALE_M, out_dir / "soil_moisture_rootzone.tif")
        except Exception as exc:  # noqa: BLE001
            print(f"  soil moisture failed: {exc}")

    if not args.skip_erosion:
        print("[erosion] R factor, slope and the RUSLE-proxy risk index")
        y0, y1 = int(args.rain_start), int(args.rain_end)
        years = ee.List.sequence(y0, y1)
        chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")

        def annual(y):
            y = ee.Number(y)
            return (chirps.filterDate(ee.Date.fromYMD(y, 1, 1),
                                      ee.Date.fromYMD(y.add(1), 1, 1)).sum())

        p_ann = ee.ImageCollection(years.map(annual)).mean()           # mean annual mm
        # R factor (Renard and Freimund): R = 0.0483 * P^1.610
        r_factor = p_ann.pow(1.610).multiply(0.0483).rename("R").clip(region)

        dem = ee.Image("USGS/SRTMGL1_003")
        slope_deg = ee.Terrain.slope(dem).clip(region)
        sin_s = slope_deg.multiply(3.141592653589793 / 180.0).sin()
        # McCool S factor: S = 10.8 sin(theta) + 0.03 for slope < 9% (theta < 5.14 deg),
        # S = 16.8 sin(theta) - 0.50 otherwise.
        s_low = sin_s.multiply(10.8).add(0.03)
        s_high = sin_s.multiply(16.8).subtract(0.50)
        s_factor = s_low.where(slope_deg.gte(5.14), s_high)
        risk = r_factor.multiply(s_factor).rename("risk").clip(region)

        try:
            fetch(r_factor, TERRAIN_SCALE_M, out_dir / "erosion_rain_r.tif")
            fetch(slope_deg, TERRAIN_SCALE_M, out_dir / "slope_deg.tif")
            fetch(risk, TERRAIN_SCALE_M, out_dir / "erosion_risk_index.tif")
        except Exception as exc:  # noqa: BLE001
            print(f"  erosion failed: {exc}")

    print("\nDone. Next: python run_all.py --layer geography   then   python analyze.py")


if __name__ == "__main__":
    main()
