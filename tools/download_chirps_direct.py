"""Download CHIRPS annual rainfall directly from the UCSB Climate Hazards Group
server (no account required), clip each raster to Kenya, and save into
data/raw/chirps_rainfall/. Use this if you would rather not go through Earth
Engine for rainfall; for NDVI use the Earth Engine script.

    pip install requests rasterio
    python tools/download_chirps_direct.py --start 2010 --end 2024

Produces data/raw/chirps_rainfall/chirps_YYYY.tif, then:
    python run_all.py --layer geography
    python analyze.py
"""
from __future__ import annotations

import argparse
import gzip
import tempfile
from pathlib import Path

BASE_URL = "https://data.chc.ucsb.edu/products/CHIRPS-2.0/global_annual/tifs/"
KENYA = (33.8, -4.8, 42.0, 5.6)   # minlon, minlat, maxlon, maxlat


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", type=int, default=2010)
    ap.add_argument("--end", type=int, default=2024)
    ap.add_argument("--base", default=".", help="path to the kenya_fnp_db root")
    args = ap.parse_args()

    import requests
    import rasterio
    from rasterio.windows import from_bounds

    out_dir = Path(args.base) / "data" / "raw" / "chirps_rainfall"
    out_dir.mkdir(parents=True, exist_ok=True)

    for y in range(args.start, args.end + 1):
        url = f"{BASE_URL}chirps-v2.0.{y}.tif.gz"
        print(f"[{y}] {url}")
        try:
            r = requests.get(url, timeout=900)
            r.raise_for_status()
            raw = gzip.decompress(r.content)
            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tf:
                tf.write(raw)
                tmp = tf.name
            with rasterio.open(tmp) as src:
                win = from_bounds(*KENYA, src.transform)
                data = src.read(1, window=win)
                prof = src.profile.copy()
                prof.update(height=data.shape[0], width=data.shape[1],
                            transform=src.window_transform(win), compress="lzw")
                op = out_dir / f"chirps_{y}.tif"
                with rasterio.open(op, "w", **prof) as dst:
                    dst.write(data, 1)
            print(f"  saved {op.name} ({data.shape[1]} x {data.shape[0]} px)")
        except Exception as exc:  # noqa: BLE001
            print(f"  {y} failed: {exc}")

    print("\nDone. Next: python run_all.py --layer geography   then   python analyze.py")


if __name__ == "__main__":
    main()
