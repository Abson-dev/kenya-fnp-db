#!/usr/bin/env python3
"""Build the 2019 census denominators and record the local census PDFs.

Downloads handled by the pipeline place the KNBS XLSX in data/raw/kphc_2019_vol1/;
the local PDFs sit in data/raw/kphc_2019_vol1/ and data/raw/census_ke_ag/. Then:

    python build_census.py

Outputs (data/processed/geography/): census_population_county.csv,
census_population_subcounty.csv, <source>_fulltext.txt, <source>_provenance.json.

Author: Aboubacar HEMA
"""
from pathlib import Path
from kenyadb import census

if __name__ == "__main__":
    census.run(Path(__file__).resolve().parent)
