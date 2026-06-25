"""Audit and fully download the AfSIS soil-chemistry bucket (anonymous public S3
over HTTPS, no credentials). It lists every object in the bucket, then downloads
every tabular file (csv, csv.gz, tsv, txt, json, xlsx) into data/raw/afsis_chem/,
gunzipping any .gz, and skips only the bulk MIR spectra. This is broader than the
pipeline handler (which takes csv/json/txt/md only), so it guarantees no tabular
chemistry is left in the bucket.

    pip install requests
    python tools/download_afsis.py --list      # just print the full inventory
    python tools/download_afsis.py             # download all tabular files

Then rebuild the soil layer:
    python run_all.py --layer soil
    python check_soil.py

Important: AfSIS Phase I is a sentinel-site survey (a few intensively sampled 10
km blocks per country), so even the complete download is spatially clustered. For
Kenya this is why the county aggregation lands in only a couple of counties. If
you need extractable P, K, Zn and Fe for all 47 counties, a gridded product such
as iSDAsoil is the right source; AfSIS is best kept as a sparse validation set.
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

BUCKET = "afsis"
ENDPOINT = f"https://{BUCKET}.s3.amazonaws.com/"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
TABULAR = (".csv", ".csv.gz", ".tsv", ".txt", ".json", ".xlsx", ".xls")


def list_keys(requests) -> list:
    keys, token = [], None
    while True:
        params = {"list-type": "2"}
        if token:
            params["continuation-token"] = token
        r = requests.get(ENDPOINT, params=params, timeout=120)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for c in root.findall("s3:Contents", NS):
            k = c.find("s3:Key", NS).text
            sz = int(c.find("s3:Size", NS).text)
            if k and not k.endswith("/"):
                keys.append((k, sz))
        trunc = root.find("s3:IsTruncated", NS)
        if trunc is not None and trunc.text == "true":
            token = root.find("s3:NextContinuationToken", NS).text
        else:
            break
    return keys


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="list the bucket, download nothing")
    ap.add_argument("--base", default=".", help="path to the kenya_fnp_db root")
    ap.add_argument("--max-mb", type=float, default=600.0,
                    help="skip individual files larger than this (the bulk spectra); use --all to override")
    ap.add_argument("--all", action="store_true", help="download every tabular file regardless of size")
    args = ap.parse_args()

    import requests

    keys = list_keys(requests)
    total = sum(s for _, s in keys)
    tab = [(k, s) for k, s in keys if k.lower().endswith(TABULAR)]
    print(f"AfSIS bucket: {len(keys)} objects, {total / 1e6:.1f} MB total")
    print(f"Tabular files in the bucket: {len(tab)}")
    for k, s in tab:
        flag = "" if (args.all or s / 1e6 <= args.max_mb) else "  [skipped: over --max-mb]"
        print(f"  {s / 1e6:8.2f} MB  {k}{flag}")
    skipped = len(keys) - len(tab)
    if skipped:
        print(f"({skipped} non-tabular objects, mostly raw MIR spectra, left in the bucket)")
    if args.list:
        return

    out = Path(args.base) / "data" / "raw" / "afsis_chem"
    out.mkdir(parents=True, exist_ok=True)
    got = 0
    for k, s in tab:
        if not args.all and s / 1e6 > args.max_mb:
            continue
        dest = out / Path(k).name
        print(f"downloading {k} ...")
        r = requests.get(ENDPOINT + k, timeout=1800)
        r.raise_for_status()
        dest.write_bytes(r.content)
        if dest.suffix == ".gz":
            with gzip.open(dest, "rb") as fin, open(dest.with_suffix(""), "wb") as fout:
                shutil.copyfileobj(fin, fout)
        got += 1
    print(f"\nDownloaded {got} file(s) -> {out}")
    print("Next: python run_all.py --layer soil   then   python check_soil.py")


if __name__ == "__main__":
    main()
