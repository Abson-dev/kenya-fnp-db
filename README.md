# Kenya Soil / Food / Nutrition / Policy database

A configuration-driven, provenance-tracked acquisition pipeline that assembles
the Kenya dataset bundle into a single layered database keyed on a master
county and sub-county crosswalk.

Author: Aboubacar HEMA

## Design in one paragraph

The bundle is treated as a layered system, not one monolithic table. A master
county / sub-county crosswalk (from COD-AB plus the 2019 census) is the spine,
and every later indicator is appended to that crosswalk rather than merged
table-to-table. Soil, food, health and policy each form a thematic schema.
Heavy artefacts (rasters, survey microdata, strategy PDFs) stay on disk as
files; the database holds the tabular indicators, the crosswalk, the flattened
source registry, and a full provenance ledger that records publisher, mirror,
licence, checksum and extraction date for every object. That mirrors the
integration rules in the bundle document: three parallel keys (spatial,
denominator, provenance) and a strict separation between gridded predictions,
legacy polygons, measured points, survey microdata and policy text.

## Layout

```
kenya_fnp_db/
  config/sources.yaml        single source of truth (25 sources, 5 layers)
  run_all.py                 CLI: acquire -> crosswalk -> build database
  kenyadb/
    pipeline.py              walks the registry, dispatches handlers
    handlers.py              one handler per access method
    crosswalk.py             master county / sub-county crosswalk
    build_db.py              assembles the DuckDB database
    utils/{io,provenance,db}.py
  data/
    raw/                     automated downloads (one folder per source)
    external/                manual / gated drops (DHS, KNMS, MICS, SoilHive...)
    interim/                 parsed intermediates (census PDF -> CSV, etc.)
    processed/               normalised, crosswalk-joined outputs
    db/kenya_fnp.duckdb      the assembled database
  logs/manifest_*.json       per-run provenance manifest
  MANUAL_DATASETS.md         step-by-step for the gated sources
```

## Quick start

```bash
pip install -r requirements.txt

python run_all.py --dry-run        # print the full plan, fetch nothing
python run_all.py --only-open      # fetch the immediately-open sources
python run_all.py --layer soil     # one layer at a time
python run_all.py                  # full run (open fetched, gated flagged)
python run_all.py --build-only     # rebuild DB from whatever is on disk
```

Each run writes `logs/manifest_<run_id>.json` and appends to the `provenance`
table inside the database, so a run is fully reconstructable after the fact.

## Access reality (the bottlenecks matter)

The bundle splits into four access types. The pipeline automates the first two
and records the rest as explicit manual gates with instructions.

| Access | Sources | Pipeline behaviour |
| --- | --- | --- |
| open_api / open_download | COD-AB, census, SoilGrids, KENSOTER, SODMA soil, AfSIS, KFCT, KilimoSTAT, FAOSTAT, WFP prices, WB real-time prices, WB HNP, GFDx, ASPIRE, ASTI, Action Plan | fetched automatically, checksummed, logged |
| registration | KDHS 2022, MICS 2000 | manual gate + rdhs recipe |
| agreement / request | KNMS 2011, SoilHive OCP | manual gate + request instructions |
| dashboard | Kenya FSD, NIPFN, MoH/KEBS fortification, FAOLEX | manual export, then dropped into data/external/ |

The four most important bottlenecks are KDHS 2022, KNMS 2011, MICS 2000 and
some SoilHive datasets. See `MANUAL_DATASETS.md`.

## The five layers

1. Geography and denominators - COD-AB admin boundaries (47 counties,
   290 sub-counties) and 2019 KPHC population, households, area and density.
   This is the master spatial and denominator key.
2. Soil - SoilGrids 250 m as the national gridded backbone (SOC, pH, N, CEC,
   bulk density, texture); KENSOTER and the SODMA legacy polygons for Kenya
   soil classes; AfSIS and SoilHive points for micronutrient enrichment
   (Zn, Fe, P, K) and calibration. Kept as three distinct tables, never
   flattened into one surface.
3. Food - KFCT 2018 nutrient composition; KilimoSTAT and FAOSTAT for
   production and supply; WFP (observed) and World Bank (modeled) prices stored
   in separate but linkable tables, tied to KFCT food items by a documented
   concordance rather than name matching.
4. Health - KDHS 2022 for current anthropometry and anemia; KNMS 2011 for the
   richer biomarkers (ferritin, RBP/vitamin A, iodine, zinc, folate); WB HNP
   for the clean country-year panel; NIPFN only as a fast dashboard accelerator.
5. Policy - FAOLEX and the 2024-2030 Action Plan as the document backbone, with
   GFDx (fortification), ASPIRE (social protection) and ASTI (agricultural R&D)
   as structured covariate layers.

## Database tables

| Table | Contents |
| --- | --- |
| core.crosswalk_admin | master county / sub-county join key (codes + normalised names) |
| core.source_registry | flattened registry: every source, publisher, access, licence, role |
| provenance | per-object ledger: path, checksum, bytes, status, extracted_at |
| geography.* food.* soil.* health.* policy.* | normalised indicators registered from data/processed/<layer>/ |

DuckDB is the default engine: no server, native CSV / Parquet / GeoPackage
reads, and a spatial extension for the geometry joins. To use PostGIS instead,
point the loaders at a libpq connection; the table layout is identical.

## Extending it

Add a source by appending an entry to `config/sources.yaml` under the right
layer with a `handler` value from `kenyadb/handlers.py`. Add a new access
method by writing a handler and registering it in the `HANDLERS` map. Drop a
normalised `<name>.csv` into `data/processed/<layer>/` and it is auto-registered
as `<layer>.<name>` on the next build.

## Caveats carried from the bundle

Several sources are mirrors or aggregators (Census.ke, the SODMA soil mirror),
so the provenance ledger always stores the original publisher, the mirror used,
the extraction date and the checksum. SoilGrids does not ship Zn / Fe / P / K as
national rasters, so micronutrients come from the Kenya point datasets. Observed
and modeled prices live in separate tables. KDHS and KNMS stay distinct
analytical modules and are never silently merged.
