# Issue Finder System for PR Writer

This repository contains a Python CLI that finds and ranks GitHub issues that match
your PR-writer constraints.

## What this tool filters for

For each candidate, the tool enforces:

1. Repository constraints
   - Python repository (`language:Python` query)
   - `stars >= 200` (configurable)
   - repo size `<= 200 MB` (configurable)
   - excludes forks by default
2. Issue constraints
   - issue is closed
   - issue text is "pure": no links and no images in title/body
   - issue body length threshold (default `>= 80` chars)
3. Issue ↔ PR linkage constraints
   - exactly **one** linked PR found in issue timeline
   - PR is merged
   - PR closing references resolve exactly one issue, and it must be that issue
4. Code-diff constraints
   - PR has at least **4 Python non-test/non-doc files** changed
   - at least one such file has "large enough" change count (default `>= 35` lines)
5. Complexity constraints
   - computes complexity score from:
     - number of Python non-test files changed
     - total Python line changes
     - commit count
     - largest single Python file change
     - issue body size
   - only keeps issues above minimum score (default `>= 50`)

## Project layout

```text
issue_finder/
  cli.py            # command-line interface
  github_client.py  # wrapper over `gh api`
  finder.py         # filtering + analysis pipeline
  filters.py        # text/path/link/reference filters
  scoring.py        # complexity scoring
  models.py         # data models for output
run_issue_finder.py # simple entrypoint
tests/              # unit tests
```

## Requirements

- Python 3.10+
- GitHub CLI authenticated in your shell (`gh auth status`)

No third-party Python dependencies are required.

## How to run

### 1) Search broadly and print top candidates

```bash
python run_issue_finder.py --top 10 --show-rejections
```

### 2) Analyze only specific repositories

```bash
python run_issue_finder.py --repos "pallets/flask,tiangolo/fastapi" --top 5
```

### 3) Export JSON

```bash
python run_issue_finder.py --top 20 --json-out output/candidates.json
```

## Useful flags

- `--min-stars 200`
- `--max-size-mb 200`
- `--min-python-files 4`
- `--min-single-file-changes 35`
- `--min-complexity-score 50`
- `--issues-per-repository 30`
- `--max-repositories 10`
- `--show-rejections`

Run full help:

```bash
python run_issue_finder.py --help
```

## Rate-limit note

GitHub API rate limits can be hit on large scans. Start with smaller values:

- `--max-repositories 5`
- `--issues-per-repository 20`

Then increase gradually if needed.

## Tests

```bash
python -m unittest discover -s tests -v
```
