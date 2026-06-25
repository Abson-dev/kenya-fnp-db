"""Download iSDAsoil gridded soil-nutrient rasters for Kenya through Google Earth
Engine, into data/raw/isda/. This fills the micronutrient gap (extractable P, K,
Zn, Fe) at full 47-county coverage, which the sparse AfSIS points cannot give.

    pip install earthengine-api requests
    earthengine authenticate
    python tools/download_isda_gee.py --project ee-aboubacarhema94
    # all properties:  python tools/download_isda_gee.py --project ... --props all

Produces, for each property, data/raw/isda/isda_<property>.tif, already
back-transformed to natural units and blended to 0-30 cm. Then:

    python run_all.py --layer soil
    python check_soil.py
    python analyze.py

Units and back-transformation
-----------------------------
iSDAsoil stores most properties log-transformed. This script applies the iSDA
back-transformation per pixel BEFORE export, so the rasters are in natural units:
  - concentration properties:  value = exp(stored / 10) - 1
  - pH:                         value = stored / 10
The 0-30 cm value is the thickness-weighted blend of the 0-20 cm and 20-50 cm
predictions: (20 * v_0_20 + 10 * v_20_50) / 30. If iSDA changes its storage
convention, adjust the transform here; the transformation is intentionally kept
in this download step and documented so the county aggregation stays a plain mean.
"""
from __future__ import annotations

import argparse
from pathlib import Path

KENYA_BBOX = [33.8, -4.8, 42.0, 5.6]
ISDA_COLLECTION = "ISDASOIL/Africa/v1"
# property -> kind ("conc" = log back-transform, "ph" = divide by 10)
ISDA_PROPERTIES = {
    "phosphorus_extractable": "conc",
    "potassium_extractable": "conc",
    "zinc_extractable": "conc",
    "iron_extractable": "conc",
    "calcium_extractable": "conc",
    "magnesium_extractable": "conc",
    "sulphur_extractable": "conc",
    "nitrogen_total": "conc",
    "carbon_organic": "conc",
    "ph": "ph",
}
DEFAULT_PROPS = ["phosphorus_extractable", "potassium_extractable",
                 "zinc_extractable", "iron_extractable"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", default=None, help="your Earth Engine Cloud project id")
    ap.add_argument("--base", default=".", help="path to the kenya_fnp_db root")
    ap.add_argument("--props", nargs="*", default=None,
                    help="properties to fetch; default is P, K, Zn, Fe. Use 'all' for every property")
    ap.add_argument("--scale", type=int, default=1000,
                    help="export resolution in metres (county means are robust at 1000; 500 for finer)")
    args = ap.parse_args()

    import ee
    import requests

    if args.project:
        ee.Initialize(project=args.project)
    else:
        ee.Initialize()

    if args.props == ["all"]:
        props = list(ISDA_PROPERTIES)
    elif args.props:
        props = args.props
    else:
        props = DEFAULT_PROPS

    region = ee.Geometry.Rectangle(KENYA_BBOX)
    out_dir = Path(args.base) / "data" / "raw" / "isda"
    out_dir.mkdir(parents=True, exist_ok=True)

    def back_transform(band, kind):
        if kind == "ph":
            return band.divide(10)
        return band.divide(10).exp().subtract(1)   # exp(x/10) - 1

    for prop in props:
        kind = ISDA_PROPERTIES.get(prop, "conc")
        print(f"[{prop}] ({kind})")
        try:
            img = ee.Image(f"{ISDA_COLLECTION}/{prop}")
            v020 = back_transform(img.select("mean_0_20"), kind)
            v2050 = back_transform(img.select("mean_20_50"), kind)
            v030 = (v020.multiply(20).add(v2050.multiply(10)).divide(30)
                    .rename(prop).clip(region))
            url = v030.getDownloadURL({"region": region, "scale": args.scale,
                                       "crs": "EPSG:4326", "format": "GEO_TIFF"})
            r = requests.get(url, timeout=900)
            r.raise_for_status()
            out = out_dir / f"isda_{prop}.tif"
            out.write_bytes(r.content)
            print(f"  saved {out.name} ({len(r.content) // 1024} KB)")
        except Exception as exc:  # noqa: BLE001
            print(f"  {prop} failed: {exc}")

    print("\nDone. Next: python run_all.py --layer soil   then   python analyze.py")


if __name__ == "__main__":
    main()
