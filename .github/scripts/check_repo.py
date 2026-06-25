#!/usr/bin/env python3
"""Static repository guards for the Kenya FNP pipeline.

These checks run without any acquired data, so they are safe in CI and in a
local pre-push hook. The full pipeline needs licensed data that is never
committed (DHS microdata and others), so it cannot run in CI; what we can
verify cheaply is:

  1. every Python module compiles (syntax only, no imports, no data),
  2. config/sources.yaml parses,
  3. the typography house style holds (no em or en dash anywhere; no straight
     apostrophe in Markdown prose),
  4. no data, log or analysis-output file has been committed.

The exit code is non-zero if any check fails, with a short report. Run it with
`python .github/scripts/check_repo.py` from the repository root.
"""
from __future__ import annotations

import py_compile
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Directories that never carry tracked source (mirrors .gitignore).
EXCLUDE_PARTS = {".git", "data", "logs", "__pycache__", ".venv", "venv", "env"}
# Generated analysis artefacts live here and are git-ignored.
EXCLUDE_PREFIXES = ("data/", "logs/", "analysis/outputs/")

# The two dash characters that the house style forbids, written as escapes so
# this guard never trips over itself.
EM_DASH = "\u2014"
EN_DASH = "\u2013"
STRAIGHT_APOSTROPHE = "'"

TEXT_EXTENSIONS = {".py", ".md", ".yaml", ".yml", ".txt", ".cff", ".sh", ".js", ".sql"}


def strip_md_code(text: str) -> str:
    """Remove fenced and inline code from Markdown so the apostrophe check sees
    only prose. Shell snippets legitimately use straight quotes; prose does not."""
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", "", text)
    return text


def repo_files():
    """Return the files git tracks; fall back to a filesystem walk off-git."""
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
        return [ROOT / line for line in out.splitlines() if line.strip()]
    except Exception:  # noqa: BLE001 - not a git checkout (for example the sandbox)
        files = []
        for p in ROOT.rglob("*"):
            if not p.is_file():
                continue
            if any(part in EXCLUDE_PARTS for part in p.parts):
                continue
            rel = p.relative_to(ROOT).as_posix()
            if rel.startswith(EXCLUDE_PREFIXES):
                continue
            files.append(p)
        return files


def main() -> int:
    files = repo_files()
    errors = []

    # 1. Python compiles.
    for p in files:
        if p.suffix == ".py":
            try:
                py_compile.compile(str(p), doraise=True)
            except py_compile.PyCompileError as exc:
                errors.append(f"compile: {p.relative_to(ROOT)}: {exc.msg}")

    # 2. The source registry parses.
    cfg = ROOT / "config" / "sources.yaml"
    if cfg.exists():
        try:
            import yaml

            yaml.safe_load(cfg.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"yaml: config/sources.yaml does not parse: {exc}")

    # 3. Typography house style.
    for p in files:
        if p.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = p.relative_to(ROOT)
        if EM_DASH in text or EN_DASH in text:
            errors.append(f"typography: {rel}: contains an em or en dash (use hyphens only)")
        if p.suffix.lower() == ".md" and STRAIGHT_APOSTROPHE in strip_md_code(text):
            errors.append(f"typography: {rel}: straight apostrophe in Markdown prose (use U+2019)")

    # 4. Data-leak guard.
    for p in files:
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(EXCLUDE_PREFIXES) and p.name != ".gitkeep":
            errors.append(f"data-leak: {rel} must not be committed")

    if errors:
        print("Repository checks FAILED:\n")
        for e in errors:
            print(f"  - {e}")
        print(f"\n{len(errors)} problem(s) found.")
        return 1

    print(
        f"All repository checks passed ({len(files)} files): "
        "compile, sources.yaml, typography, data-leak."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
