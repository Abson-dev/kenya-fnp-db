"""Archive extraction.

Several handlers download zipped payloads (COD-AB shapefiles, KENSOTER, some
CKAN resources). The shapefile / CSV we need lives inside the archive, so we
extract every archive in data/raw into a sibling _extracted/ folder before the
crosswalk and transforms run. Idempotent: an archive whose marker file already
exists is skipped.

Author: Aboubacar HEMA
"""
from __future__ import annotations

import gzip
import shutil
import zipfile
from pathlib import Path


def _extract_zip(archive: Path, dest: Path) -> int:
    n = 0
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            zf.extract(member, dest)
            n += 1
    return n


def _extract_gz(archive: Path, dest: Path) -> int:
    out = dest / archive.with_suffix("").name
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(archive, "rb") as fin, open(out, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    return 1


def extract_all(raw_dir: Path) -> None:
    """Walk data/raw and extract every .zip / .gz into <name>_extracted/.

    FAOSTAT normalised zips are left alone: the FAOSTAT transform reads them
    in place with pandas, so unpacking them would only duplicate large files.
    """
    raw_dir = Path(raw_dir)
    if not raw_dir.exists():
        return
    archives = list(raw_dir.rglob("*.zip")) + list(raw_dir.rglob("*.gz"))
    for arc in archives:
        if "_extracted" in arc.parts:
            continue
        if arc.suffix == ".zip" and arc.stem.endswith("_normalized"):
            continue  # FAOSTAT bulk read in place
        dest = arc.parent / f"{arc.stem}_extracted"
        marker = dest / ".extracted"
        if marker.exists():
            continue
        dest.mkdir(parents=True, exist_ok=True)
        try:
            n = _extract_zip(arc, dest) if arc.suffix == ".zip" else _extract_gz(arc, dest)
            marker.write_text("ok", encoding="utf-8")
            print(f"[extract] {arc.name} -> {dest.name} ({n} files)")
        except Exception as exc:  # noqa: BLE001
            print(f"[extract] skipped {arc.name}: {exc}")
