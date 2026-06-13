"""Master county / sub-county crosswalk.

The bundle document's central design rule: keep ONE master county and
sub-county crosswalk derived from COD-AB plus census names and codes, and
append every later table to that crosswalk rather than merging table-to-table.
This module builds that crosswalk.

Strategy:
  1. Seed with the canonical 47 KNBS counties (codes 1-47), which are stable.
  2. If a COD-AB admin-2 layer has been downloaded, enrich the crosswalk with
     the 290 sub-counties and their pcodes, joining on a normalised county name.
  3. Emit data/processed/crosswalk_admin.csv as the join key for all layers.

Reading the COD-AB geometry needs geopandas; when it is not installed the
county-level seed is still produced so the rest of the pipeline can proceed.

Author: Aboubacar HEMA
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

# Canonical KNBS county codes (1-47) and names - the stable spatial spine.
COUNTIES: list[tuple[int, str]] = [
    (1, "Mombasa"), (2, "Kwale"), (3, "Kilifi"), (4, "Tana River"),
    (5, "Lamu"), (6, "Taita Taveta"), (7, "Garissa"), (8, "Wajir"),
    (9, "Mandera"), (10, "Marsabit"), (11, "Isiolo"), (12, "Meru"),
    (13, "Tharaka Nithi"), (14, "Embu"), (15, "Kitui"), (16, "Machakos"),
    (17, "Makueni"), (18, "Nyandarua"), (19, "Nyeri"), (20, "Kirinyaga"),
    (21, "Murang'a"), (22, "Kiambu"), (23, "Turkana"), (24, "West Pokot"),
    (25, "Samburu"), (26, "Trans Nzoia"), (27, "Uasin Gishu"),
    (28, "Elgeyo Marakwet"), (29, "Nandi"), (30, "Baringo"), (31, "Laikipia"),
    (32, "Nakuru"), (33, "Narok"), (34, "Kajiado"), (35, "Kericho"),
    (36, "Bomet"), (37, "Kakamega"), (38, "Vihiga"), (39, "Bungoma"),
    (40, "Busia"), (41, "Siaya"), (42, "Kisumu"), (43, "Homa Bay"),
    (44, "Migori"), (45, "Kisii"), (46, "Nyamira"), (47, "Nairobi"),
]


def norm(name: str) -> str:
    """Normalise a county/sub-county name for joins: lowercase, strip accents
    of apostrophes, collapse separators."""
    s = name.strip().lower()
    s = s.replace("\u2019", "'")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def _find_codab_admin2(raw_dir: Path) -> Path | None:
    """Locate a downloaded COD-AB admin-2 vector file (shp/gpkg)."""
    cod_dir = raw_dir / "cod_ab"
    if not cod_dir.exists():
        return None
    patterns = ["*adm2*.shp", "*ADM2*.shp", "*adm2*.gpkg", "*ADM2*.gpkg",
                "*adm2*.json", "*adm2*.geojson"]
    for pat in patterns:
        hits = sorted(cod_dir.rglob(pat))
        if hits:
            return hits[0]
    return None


def build(raw_dir: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "crosswalk_admin.csv"

    rows: list[dict] = []
    admin2 = _find_codab_admin2(raw_dir)
    enriched = False

    if admin2 is not None:
        try:
            import geopandas as gpd  # type: ignore

            gdf = gpd.read_file(admin2)
            # Try to guess the county / sub-county / pcode columns.
            cols = {c.lower(): c for c in gdf.columns}
            c_name = next((cols[c] for c in cols if "adm1" in c and "name" in c
                           or c in ("county", "adm1_en")), None)
            s_name = next((cols[c] for c in cols if "adm2" in c and "name" in c
                           or c in ("subcounty", "sub_county", "adm2_en")), None)
            c_code = next((cols[c] for c in cols if "adm1" in c and ("pcode" in c or "code" in c)), None)
            s_code = next((cols[c] for c in cols if "adm2" in c and ("pcode" in c or "code" in c)), None)
            if c_name and s_name:
                for _, r in gdf.iterrows():
                    cn = str(r[c_name])
                    rows.append({
                        "county_code": str(r[c_code]) if c_code else "",
                        "county_name": cn,
                        "county_norm": norm(cn),
                        "subcounty_code": str(r[s_code]) if s_code else "",
                        "subcounty_name": str(r[s_name]),
                        "subcounty_norm": norm(str(r[s_name])),
                        "source": "COD-AB admin2",
                    })
                enriched = True
        except Exception as exc:  # noqa: BLE001
            print(f"[crosswalk] could not read COD-AB admin2 ({exc}); using county seed")

    if not enriched:
        for code, name in COUNTIES:
            rows.append({
                "county_code": f"KE{code:02d}",
                "county_name": name,
                "county_norm": norm(name),
                "subcounty_code": "",
                "subcounty_name": "",
                "subcounty_norm": "",
                "source": "KNBS county seed (run geography layer to add sub-counties)",
            })

    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    n_counties = len({r["county_norm"] for r in rows})
    print(f"[crosswalk] wrote {out} ({len(rows)} rows, {n_counties} counties, "
          f"{'sub-county enriched' if enriched else 'county seed only'})")
    return out
