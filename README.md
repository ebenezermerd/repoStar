# repoStar

`repoStar` is a small CLI that helps you **find GitHub issues + PRs** that match “PR-writer training” constraints (Python repos, closed issues, exactly one closing PR, repo size limit, minimum number of non-test/non-doc Python files changed, etc.).

## Setup

```bash
python3 -m pip install -r requirements.txt
```

You must provide a GitHub token (fine-grained or classic) either via env var or CLI flag.

```bash
export GITHUB_TOKEN="..."  # or GH_TOKEN
```

## Run

Basic run (writes `out/candidates.csv` and `out/candidates.json`):

```bash
python3 -m issue_finder.cli
```

Or pass the token explicitly:

```bash
python3 -m issue_finder.cli --github-token "..."
```

Tune constraints:

```bash
python3 -m issue_finder.cli \
  --min-stars 200 \
  --max-repos 80 \
  --max-issues-per-repo 50 \
  --min-non-test-doc-py-files 4 \
  --min-max-file-changes 60 \
  --max-repo-size-mb 200 \
  --require-pure-text \
  --reject-screenshot-mentions \
  --require-merged-pr \
  --out-dir out
```

## What it checks (current heuristics)

- **Repo**: `language:Python`, `stars >= --min-stars`, `archived:false`, `fork:false`, `size <= --max-repo-size-mb`
- **Issue**: `is:issue is:closed`, and issue text is *roughly “pure text”* (configurable via `--require-pure-text/--no-require-pure-text` and `--reject-screenshot-mentions/--no-reject-screenshot-mentions`)
- **One-way linkage**:
  - the issue must be closed by **exactly one PR**
  - that PR must have **exactly one** `closingIssuesReferences` (the issue itself)
  - the PR must be **merged** (configurable via `--require-merged-pr/--no-require-merged-pr`)
- **PR change shape**:
  - changed files must include **at least** `--min-non-test-doc-py-files` `.py` files that are **not** tests/docs
  - at least one such file must have `--min-max-file-changes` or more “changes”
- **Outputs**: includes `base_sha` (PR `base.sha`) so you can check out the “before fix” state.

## Notes / limitations

- GitHub API rate-limits apply. If you hit rate limits, reduce `--max-repos` / `--max-issues-per-repo` or retry later.
- “Pure text” detection is intentionally strict; it may exclude some valid issues.
- The “4+ Python files changed” rule is based on PR file lists and uses simple path heuristics to exclude tests/docs.

