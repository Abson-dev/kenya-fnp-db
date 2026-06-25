# GitHub workflow for the Kenya FNP pipeline

A practical guide to publishing this project on GitHub and keeping it up to date.

The one rule that shapes everything below: **the acquired data is never committed.** The repository ships the code that fetches and builds the data, not the data itself, because several sources (DHS microdata, KNMS, SoilHive) sit under data-use agreements that forbid redistribution. The `.gitignore`, the continuous-integration pipeline and the pre-push hook all exist to keep that rule.

Replace `Abson-dev/kenya_fnp_db` below with your own account and repository name if they differ.

## Contents

1. [The golden rule: no data in git](#1-the-golden-rule-no-data-in-git)
2. [One-time setup](#2-one-time-setup)
3. [The safety net](#3-the-safety-net)
4. [Everyday workflow](#4-everyday-workflow)
5. [Updating a working copy](#5-updating-a-working-copy)
6. [The CI pipeline](#6-the-ci-pipeline)
7. [The local pre-push hook](#7-the-local-pre-push-hook)
8. [Releases and versioning](#8-releases-and-versioning)
9. [Command cheat-sheet](#9-command-cheat-sheet)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. The golden rule: no data in git

Three trees are excluded from version control by `.gitignore`: `data/` (every raw, external, interim, processed and database file), `logs/` (run manifests) and `analysis/outputs/` (regenerated artefacts). Credentials (`.rdhs.json`, anything matching `*secret*` or `*credentials*`) are excluded too. Folder structure is preserved with `.gitkeep` files so a fresh clone still has the right layout.

A collaborator reproduces the data by running the pipeline and completing the manual steps in `MANUAL_DATASETS.md`, not by cloning files. If you ever feel tempted to commit a data file so that someone can skip a download, do not: it is almost always a licence violation and it bloats the history permanently.

---

## 2. One-time setup

Create the empty repository on GitHub first (no README, no licence, no `.gitignore`, since the project already ships those), then from the project root:

```bash
# 1. initialise and make the first commit
git init
git add .
git status                 # read this carefully (see the safety check below)
git commit -m "Initial commit: Kenya soil-food-body pipeline"

# 2. point at your GitHub repository and push
git branch -M main
git remote add origin https://github.com/Abson-dev/kenya_fnp_db.git
git push -u origin main
```

**Before that first `git push`, confirm no data is staged.** Two quick checks:

```bash
# nothing under data/, logs/ or analysis/outputs/ should appear (only .gitkeep)
git ls-files | grep -E '^(data|logs|analysis/outputs)/' | grep -v '.gitkeep'

# or run the repository guard, which checks this and more
python .github/scripts/check_repo.py
```

If the first command prints any path, stop and fix `.gitignore` before pushing. If the guard prints "All repository checks passed", you are clear to push.

---

## 3. The safety net

Three things protect the repository, and they share one script so they never drift apart.

`.github/scripts/check_repo.py` is the single source of truth. Run from the repository root, it verifies four things and needs no data:

- every Python module compiles (syntax only, no imports, no downloads),
- `config/sources.yaml` parses,
- the typography house style holds (no em or en dash anywhere; no straight apostrophe in Markdown prose),
- no data, log or output file has been committed.

It exits non-zero with a short report if anything is wrong, which is what lets both the CI pipeline and the pre-push hook fail fast. You can run it by hand any time:

```bash
python .github/scripts/check_repo.py
```

---

## 4. Everyday workflow

Work on a branch, never directly on `main`, so every change is reviewable and `main` stays releasable.

```bash
# start a change
git checkout -b feature/cob-expenditure-ingest

# ... edit code ...

# check before you commit
python .github/scripts/check_repo.py

# stage, commit, push the branch
git add -A
git commit -m "Add Controller of Budget expenditure ingest to the policy panel"
git push -u origin feature/cob-expenditure-ingest
```

Then open a pull request on GitHub (the page offers a button once the branch is pushed). CI runs automatically on the pull request. When the checks are green and the change is reviewed, merge it into `main` and delete the branch.

Good commit messages describe the effect, not the file: "Add the 2014 KDHS round and the stunting trend", not "update transforms.py".

---

## 5. Updating a working copy

To bring a clone up to date with `main`:

```bash
git checkout main
git pull --rebase origin main
```

Your local `data/` tree is untouched by any pull, because git does not track it. After pulling code that changes a transform or a model, rebuild from the data already on disk:

```bash
python run_all.py --build-only      # rebuild the database from local files
python analyze.py                   # regenerate the analysis outputs
```

---

## 6. The CI pipeline

The workflow lives at `.github/workflows/ci.yml` and runs on every push to `main` and on every pull request.

It deliberately runs **static checks only**. The full pipeline needs licensed data that is never in the repository, so CI cannot run `run_all.py` or `validate.py` end to end. What it does run is the `static-checks` job: it sets up Python, installs only PyYAML, and runs `.github/scripts/check_repo.py`. That is fast, needs no data, and catches the mistakes that actually happen: a syntax error, a broken registry, a stray em dash, or an accidentally committed data file.

Reading a failure: open the failed run on the Actions tab, expand the "Repository guards" step, and the report names each problem with its file. Fix locally, re-run the guard until it passes, then push again.

There is a second, optional `smoke-test` job in the same file, disabled with `if: false`. It installs the full requirements, imports the package and resolves the run plan with `run_all.py --dry-run` (still no downloads). It is off by default because the geospatial wheels are slow to install. To turn it on, change `if: false` to `if: true` or delete that line.

---

## 7. The local pre-push hook

The hook runs the same guard before each push, so a problem is caught on your machine rather than in CI. Install it once, from the repository root:

```bash
cp .github/hooks/pre-push .git/hooks/pre-push
chmod +x .git/hooks/pre-push
```

From then on, `git push` runs the checks first and aborts the push if any fail. In the rare case you need to bypass it (for example pushing a documentation-only branch you have already checked), use `git push --no-verify`.

---

## 8. Releases and versioning

Tag a meaningful state so it can be cited and returned to:

```bash
git tag -a v1.0.0 -m "First public release: soil-food-body pipeline, 30 sources"
git push origin v1.0.0
```

Then, on GitHub, draft a release from the tag with a short summary of what the version contains. When you cut a release, update `date-released` in `CITATION.cff` so the citation metadata matches. Use semantic versioning: a new data layer or model is a minor bump (1.1.0), a breaking change to the schema or the CLI is a major bump (2.0.0), and a fix is a patch (1.0.1).

---

## 9. Command cheat-sheet

```bash
# setup (once)
git init && git add . && git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/Abson-dev/kenya_fnp_db.git
git push -u origin main

# the safety guard (any time)
python .github/scripts/check_repo.py

# install the pre-push hook (once)
cp .github/hooks/pre-push .git/hooks/pre-push && chmod +x .git/hooks/pre-push

# a change
git checkout -b feature/my-change
git add -A && git commit -m "Describe the effect"
git push -u origin feature/my-change

# update a clone
git checkout main && git pull --rebase origin main

# release
git tag -a v1.1.0 -m "Add the 2014 round and Stage 5" && git push origin v1.1.0
```

---

## 10. Troubleshooting

**A data file was committed by mistake.** If it is only staged (not yet committed), `git restore --staged <path>` removes it from the commit. If it was already committed but not yet pushed, amend or reset the commit. If it was pushed, it is in history and must be purged with a history-rewrite tool such as `git filter-repo`, after which you force-push and rotate any exposed credential. Prevention is far cheaper than the cure, which is the whole point of the pre-push hook.

**Authentication.** GitHub no longer accepts account passwords over HTTPS. Use a personal access token as the password when prompted, or switch the remote to SSH:

```bash
git remote set-url origin git@github.com:Abson-dev/kenya_fnp_db.git
```

**A file is too large to push.** It is almost certainly a data or output file that should have been ignored. Confirm it is matched by `.gitignore`, remove it from the index with `git rm --cached <path>`, and commit the removal.

**The guard flags a dash you cannot see.** Em (U+2014) and en (U+2013) dashes look like long hyphens. Search and replace them with a plain hyphen; the report names the file so you know where to look.
