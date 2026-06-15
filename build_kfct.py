#!/usr/bin/env python3
"""Extract the Kenya Food Composition Tables 2018 from the local PDF.

Place the PDF at data/raw/kfct_2018/<file>.pdf, then:

    python build_kfct.py

Outputs (data/processed/food/): kfct_proximates.csv, kfct_minerals.csv,
kfct_vitamins.csv, kfct_foods.csv, kfct_provenance.json. Needs: pip install pdfplumber

Author: Aboubacar HEMA
"""
from pathlib import Path
from kenyadb import kfct

if __name__ == "__main__":
    kfct.run(Path(__file__).resolve().parent)
