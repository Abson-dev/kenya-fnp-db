# Manual and gated datasets

These sources cannot be fully automated because they sit behind registration,
a data-use agreement, or a browser-only dashboard. The pipeline records each
one as a `manual` row in the provenance ledger with the note below. Once you
have the files, drop them in the indicated `data/external/<source>/` folder and
run `python run_all.py --build-only` to fold them into the database.

Author: Aboubacar HEMA

## KDHS 2022 (registration) - data/external/kdhs_2022/

The Kenya Demographic and Health Survey 2022 is free for legitimate research
but requires a DHS account and per-project dataset approval.

1. Create an account at dhsprogram.com and register a research project.
2. Request the Kenya 2022 datasets (Household, Women, Men, Children, Births).
3. Once approved, download the Stata or SPSS files, or pull them in R:

```r
library(rdhs)
set_rdhs_config(email = "you@example.org", project = "Your project name")
get_datasets(c("KEHR8BFL", "KEPR8BFL", "KEKR8BFL", "KEIR8BFL", "KEMR8BFL"))
```

4. Place the extracted .DTA / .SAV files in `data/external/kdhs_2022/`.

County estimates: once the recodes are in place, the `kdhs_county` transform
runs automatically on the next build. It reads the children’s recode (KR:
`KEKR8xFL`) for stunting, wasting and underweight, and the women’s recode
(IR: `KEIR8xFL`) for maternal BMI and the dietary-diversity and household
controls, computes survey-weighted (v005/1e6) per-county prevalence, picks the
county variable by matching its value labels to the crosswalk, and writes
`health.kdhs_county` and `health.kdhs_controls_county`. The anaemia path stays
in the code but finds nothing, because the 2022 round carried no haemoglobin
module. Requires `pip install pyreadstat`. If a future recode renames
variables, override the defaults via the `_KDHS_VARS` map in
`kenyadb/transforms.py`.

## KDHS 2014 (registration) - data/external/kdhs_2014/

The Kenya DHS 2014 (phase 72) is the second county-representative round and is
obtained the same way as 2022, through a DHS account and project approval.

1. From your approved DHS project, download the Kenya 2014 Standard DHS recodes
   (the `KExx72DT` folders for the children, women and household-member files).
2. Drop the `KExx72DT` folders into `data/external/kdhs_2014/` (the loader
   searches subfolders, so the folders can be placed as downloaded).
3. Run `python run_all.py --layer health` (or `--build-only`).

The same parametrized transform produces `health.kdhs_county_2014` and
`health.kdhs_controls_county_2014`, which give a genuine second time point and
the 2014 to 2022 stunting trend. Note that, like 2022, the 2014 Kenya round did
not measure haemoglobin, so it adds no anaemia; Kenyan anaemia was carried only
by the 2010 and 2015 Malaria Indicator Surveys, which are not
county-representative and are therefore kept out of the county panel.

## KNMS 2011 (agreement) - data/external/knms_2011/

The Kenya National Micronutrient Survey 2011 has open documentation but the
microdata is released on request.

1. Open the KNBS NADA record for KEN-KNBS-KNMS-2011-v1.0.
2. Submit a microdata access request describing the intended use.
3. On approval, download the biomarker files (hemoglobin, ferritin, RBP /
   vitamin A, urinary iodine, serum zinc, folate / B12) plus anthropometry and
   the 24-hour recall files.
4. Place them in `data/external/knms_2011/`.

This is the biologically richest micronutrient source; keep it as a distinct
module from KDHS rather than merging the two.

## MICS 2000 (registration) - data/external/mics_2000/

Kenya MICS2 (2000), 9,300 households, is a historical complement.

1. Request access through the UNICEF MICS portal or the KNBS NADA catalog.
2. Download the child and women datasets.
3. Place them in `data/external/mics_2000/`.

## SoilHive OCP Kenya clusters (agreement) - data/external/soilhive/

SoilHive metadata is open, but cluster point data may require a request or a
data-use agreement.

1. Open the OCP Kenya dataset page on soilhive.ag.
2. Where a dataset is access-controlled, submit the request; where it is open,
   download the Cluster 1 (5,107 points) and Cluster 2 (8,616 points) tables.
3. Place CSVs in `data/external/soilhive/`. Expected columns include pH, N, P,
   K, Fe, Zn, CEC, organic carbon, Ca, Mg, Cu, S, clay and sand at 0-20 cm.

These points are the primary micronutrient enrichment for the soil layer, since
SoilGrids does not provide Zn / Fe / P / K national rasters.

## World Bank Real-Time Food Prices (registration) - data/external/wb_rtfp/

The modeled price series has no stable direct download URL.

1. Open the Real Time Food Prices study on the World Bank Microdata Library
   (WLD_2021_RTFP_v02_M).
2. Use Get Microdata to download either the Kenya file (KEN_2021_RTFP_v02_M) or
   the global by-country file (WLD_2021_RTFP-CTRY_v02_M).
3. Drop the CSV in data/external/wb_rtfp/ and run python run_all.py --build-only.

The prices transform reads it and filters to KEN, writing food.prices_wb_modeled
alongside the observed WFP prices in food.prices_wfp_observed.

## Dashboards (manual export) - data/external/<source>/

These are browser dashboards. Export the relevant tables, then drop the CSVs in
the matching folder.

- Kenya Food Systems Dashboard (fsd.kilimo.go.ke) -> data/external/fsd/
- NIPFN nutrition dashboard (nipfn.knbs.or.ke) -> data/external/nipfn/
- MoH and KEBS fortification references -> data/external/fortification/
  (mandatory nutrients: wheat flour and dry-milled maize with zinc, iron and
  vitamin A; vegetable fats and oils with vitamin A)
- FAOLEX Kenya profile -> data/external/faolex/
  (export the food/nutrition, land/soil, water, environment and social-
  protection record lists, with abstracts and full-text PDFs where available)

## After dropping files

Add a small transform that normalises each external file and writes it to
`data/processed/<layer>/<name>.csv`, joined to `core.crosswalk_admin` where the
data is sub-national. The build step then registers it automatically as
`<layer>.<name>`. Record the original publisher, the access route and the
download date so the provenance ledger stays complete.
