#!/usr/bin/env python3
"""Extract the Kenya Food Systems and Land Use Action Plan into structured
outputs, independently of a full pipeline run.

Place the PDF at data/raw/action_plan/<file>.pdf, then:

    python build_action_plan.py

Outputs (all local):
  data/processed/action_plan/Kenya action plan structured.xlsx
  data/processed/action_plan/action_plan_fulltext.txt
  data/processed/action_plan/action_plan_provenance.json
  data/external/action_plan/*.csv        (folded into the policy layer on build)

Optional, for full text + verification:  pip install pdfplumber

Author: Aboubacar HEMA
"""
from pathlib import Path

from kenyadb import action_plan

if __name__ == "__main__":
    action_plan.run(Path(__file__).resolve().parent)
