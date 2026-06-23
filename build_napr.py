#!/usr/bin/env python3
"""Extract crop area and production by county from the KNBS National Agriculture
Production Report (NAPR) 2024.

Place the report at data/raw/kilimostat/National-Agriculture-Production-Report-2024.pdf,
then:

    python build_napr.py

Output (data/processed/food/): napr_crop_county.csv (tidy: county, crop, year,
area_ha, production_mt) and napr_provenance.json. Needs: pip install pdfplumber

If your copy paginates differently and nothing is parsed, pass the offset
explicitly, e.g. napr.run(BASE, page_offset=17).

Author: Aboubacar HEMA
"""
from pathlib import Path
from kenyadb import napr

if __name__ == "__main__":
    napr.run(Path(__file__).resolve().parent)
