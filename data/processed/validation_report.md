# Kenya FNP database - consistency report

Author: Aboubacar HEMA

Summary: {'PASS': 9, 'WARN': 2, 'FAIL': 0, 'INFO': 19}

## Tables loaded
[INFO] core.crosswalk_admin
[INFO] core.source_registry
[INFO] food.faostat_kenya
[INFO] food.prices_wfp_observed
[INFO] health.wb_hnp_panel
[INFO] soil.soilgrids_zonal_county

## Crosswalk integrity
[PASS] distinct counties = 47 (expected 47)
[PASS] distinct sub-counties = 290 (expected ~290)
[PASS] duplicate county/sub-county keys = 0

## County-name join coverage
[PASS] food.prices_wfp_observed: 100.0% of rows match a county (col 'county_name')
[PASS] soil.soilgrids_zonal_county: 100.0% of rows match a county (col 'county_name')

## SoilGrids zonal statistics
[PASS] county rows = 47 (expected 47)
[INFO] coverages = 54
[PASS] no null county cells

## FAOSTAT Kenya
[PASS] area values = ['Kenya'] (expected only Kenya)
[INFO] domains = ['FBS', 'FO', 'PP', 'QCL', 'QV', 'RFN', 'RL']
[INFO] year span = 1961-2025
[WARN] null Value rows = 731 of 98757

## WFP observed prices
[INFO] rows = 26745
[INFO] date span = 2006-01-15 to 2026-04-15
[PASS] non-numeric price values = 0 (HXL tag row should be stripped)
[INFO] admin1 values matching counties = 1 of 7 distinct

## World Bank HNP panel
[INFO] indicators = 5: SH.ANM.ALLW.ZS, SH.ANM.CHLD.ZS, SH.STA.STNT.ZS, SH.STA.WAST.ZS, SN.ITK.DEFC.ZS
[INFO] year span = 1987-2023

## Provenance
[INFO] failed: 49 rows
[INFO] manual: 30 rows
[INFO] ok: 285 rows
[INFO] skipped: 7 rows
[WARN] sources with failures: census_ke_ag, faostat, kensoter, kenya_soil_mirror, kphc_2019_vol1, soilgrids, wb_rtfp
[INFO] manual gates pending: faolex, fortification_refs, fsd_food, gfdx, kdhs_2022, kilimostat, knms_2011, mics_2000, nipfn, soilhive_ocp, wb_rtfp