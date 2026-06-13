"""Master county / sub-county crosswalk.

Design rule from the bundle document: keep ONE master county and sub-county
crosswalk derived from COD-AB plus census names/codes, and append every later
table to it rather than merging table-to-table.

Boundary-layer detection is content-based, not filename-based: COD-AB releases
name their layers inconsistently, so instead of guessing from file names we
read each vector under data/raw/cod_ab and classify it by feature count
(about 47 features = counties = admin1, about 290 = sub-counties = admin2) and
by its attribute columns. find_admin_layers() is shared with the SoilGrids
zonal-statistics transform so both use the same detected boundaries.

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

# Expected feature counts and tolerance bands.
ADM1_TARGET, ADM2_TARGET = 47, 290
ADM1_BAND, ADM2_BAND = (40, 60), (240, 340)


def norm(name: str) -> str:
    s = str(name).strip().lower().replace("\u2019", "'")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def _candidate_vectors(cod_dir: Path) -> list[tuple[Path, str | None]]:
    """Return (path, layer) pairs for every readable vector under cod_dir.
    For File Geodatabases the layers are enumerated; for flat files layer=None.
    """
    out: list[tuple[Path, str | None]] = []
    if not cod_dir.exists():
        return out
    flat: list[Path] = []
    for ext in ("*.shp", "*.geojson", "*.json", "*.gpkg"):
        flat += list(cod_dir.rglob(ext))
    out += [(p, None) for p in flat]
    for gdb in cod_dir.rglob("*.gdb"):
        if gdb.is_dir():
            try:
                import fiona  # type: ignore
                for lyr in fiona.listlayers(str(gdb)):
                    out.append((gdb, lyr))
            except Exception:  # noqa: BLE001
                out.append((gdb, None))
    return out


def _classify(n: int) -> str | None:
    if ADM1_BAND[0] <= n <= ADM1_BAND[1]:
        return "adm1"
    if ADM2_BAND[0] <= n <= ADM2_BAND[1]:
        return "adm2"
    return None


def _guess_cols(gdf):
    """Return (adm1_name, adm2_name, adm1_code, adm2_code) best-guess columns."""
    cols = list(gdf.columns)

    def pick(preds):
        for c in cols:
            cl = c.lower()
            if any(p(cl) for p in preds):
                return c
        return None

    adm1_name = pick([lambda c: "adm1" in c and ("en" in c or "name" in c),
                      lambda c: c in ("county", "county_nam", "counties")])
    adm2_name = pick([lambda c: "adm2" in c and ("en" in c or "name" in c),
                      lambda c: c in ("subcounty", "sub_county", "scounty", "sub_count")])
    adm1_code = pick([lambda c: "adm1" in c and ("pcode" in c or "code" in c)])
    adm2_code = pick([lambda c: "adm2" in c and ("pcode" in c or "code" in c)])
    return adm1_name, adm2_name, adm1_code, adm2_code


def find_admin_layers(raw_dir: Path, cod_source: str = "cod_ab") -> dict:
    """Inspect every COD-AB vector and return the best admin1 / admin2 layers.

    Returns {'adm1': {...}, 'adm2': {...}} where each value carries path, layer,
    feature count and detected columns. Missing levels are absent from the dict.
    """
    import geopandas as gpd  # type: ignore

    cod_dir = Path(raw_dir) / cod_source
    found: dict[str, dict] = {}
    print(f"[crosswalk] scanning {cod_dir} for boundary layers")
    for path, layer in _candidate_vectors(cod_dir):
        try:
            gdf = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
        except Exception:  # noqa: BLE001
            continue
        n = len(gdf)
        level = _classify(n)
        if level is None:
            continue
        tag = f"{path.name}" + (f"::{layer}" if layer else "")
        target = ADM1_TARGET if level == "adm1" else ADM2_TARGET
        prev = found.get(level)
        if prev is None or abs(n - target) < abs(prev["count"] - target):
            a1n, a2n, a1c, a2c = _guess_cols(gdf)
            found[level] = {"path": str(path), "layer": layer, "count": n,
                            "adm1_name": a1n, "adm2_name": a2n,
                            "adm1_code": a1c, "adm2_code": a2c}
            print(f"[crosswalk]   {tag}: {n} features -> {level}")
    return found


def build(raw_dir: Path, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "crosswalk_admin.csv"

    rows: list[dict] = []
    enriched = False
    try:
        import geopandas as gpd  # type: ignore

        layers = find_admin_layers(raw_dir)
        adm2 = layers.get("adm2")
        if adm2 and adm2["adm2_name"]:
            gdf = (gpd.read_file(adm2["path"], layer=adm2["layer"])
                   if adm2["layer"] else gpd.read_file(adm2["path"]))
            cn, sn = adm2["adm1_name"], adm2["adm2_name"]
            cc, sc = adm2["adm1_code"], adm2["adm2_code"]
            for _, r in gdf.iterrows():
                county = str(r[cn]) if cn else ""
                rows.append({
                    "county_code": str(r[cc]) if cc else "",
                    "county_name": county,
                    "county_norm": norm(county),
                    "subcounty_code": str(r[sc]) if sc else "",
                    "subcounty_name": str(r[sn]),
                    "subcounty_norm": norm(str(r[sn])),
                    "source": f"COD-AB ({Path(adm2['path']).name})",
                })
            enriched = bool(rows)
            if not enriched:
                print("[crosswalk] adm2 layer found but yielded no rows")
        else:
            print("[crosswalk] no admin2 layer detected (need ~290 features with a "
                  "sub-county name column); falling back to county seed")
    except ImportError:
        print("[crosswalk] geopandas not installed; using county seed")
    except Exception as exc:  # noqa: BLE001
        print(f"[crosswalk] boundary read failed ({exc}); using county seed")

    if not enriched:
        for code, name in COUNTIES:
            rows.append({
                "county_code": f"KE{code:02d}", "county_name": name,
                "county_norm": norm(name), "subcounty_code": "",
                "subcounty_name": "", "subcounty_norm": "",
                "source": "KNBS county seed",
            })

    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    n_counties = len({r["county_norm"] for r in rows if r["county_norm"]})
    print(f"[crosswalk] wrote {out} ({len(rows)} rows, {n_counties} counties, "
          f"{'sub-county enriched' if enriched else 'county seed only'})")
    return out
