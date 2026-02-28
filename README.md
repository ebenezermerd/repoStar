# Issue Finder for PR Writer HFI Project

A tool to find and analyze GitHub issues that fit the **PR Writer HFI (Human Feedback Interface)** project criteria. Use it to discover repositories and issues suitable for AI coding evaluation—beyond the pre-selected spreadsheet—with precise filtering and complexity analysis.

## Criteria (from PR Writer Guidelines)

### Repository
- **Python** git repository
- **≤ 200 MB** size (clone size)
- **≥ 200 stars**
- Excludes: collection, list, guide, projects, exercises
- High-quality, open source

### Issue
- **Closed** and linked to a PR that closes it
- **Pure description**: no images, no links in the issue body
- **One-way closure**: PR closes only this issue (no other issues)
- **≥ 4 Python code files changed** (excluding test and documentation files)
- **Substantial changes**: at least one non-test Python file has ≥ 5 lines changed
- **Well-scoped**: not too trivial (model shouldn’t solve in 1–2 turns), not too broad

## Installation

```bash
pip install -r requirements.txt
```

**GitHub token recommended**: Without a token, the API allows ~60 requests/hour. With `GITHUB_TOKEN` you get ~5,000/hour. Set it before running:

```bash
export GITHUB_TOKEN=ghp_xxx
```

## Usage

### Search across GitHub

```bash
# Basic search (uses GITHUB_TOKEN if set for higher rate limits)
python -m issue_finder

# With options
python -m issue_finder --min-stars 200 --max-repos 100 --min-score 5.0

# Export to JSON and CSV
python -m issue_finder --json results.json --csv results.csv

# Exclude issues you've already taken (from spreadsheet)
python -m issue_finder --excluded excluded.txt
```

### Analyze a single repository

```bash
python -m issue_finder --repo owner/repo
```

### Excluded issues file

Create a text file with one issue per line (URL or `owner/repo#123`):

```
https://github.com/owner/repo/issues/42
owner/repo#123
```

Lines starting with `#` are ignored.

## CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--token` | `GITHUB_TOKEN` | GitHub API token |
| `--min-stars` | 200 | Minimum repository stars |
| `--max-repos` | 50 | Max repositories to scan |
| `--max-issues-per-repo` | 100 | Max closed issues per repo |
| `--excluded` | - | File with excluded issue IDs |
| `--min-score` | 5.0 | Minimum analysis score |
| `--json` | - | Output path for JSON |
| `--csv` | - | Output path for CSV |
| `--repo` | - | Analyze single repo (owner/repo) |

## Output

- **Console**: Rich table with repo, stars, issue, score, files changed, complexity
- **JSON**: Full results including `base_sha`, `reasons`, `pr_url`, etc.
- **CSV**: Flattened table for spreadsheets

### Base SHA

The `base_sha` field is the commit to checkout before the fix (pre-issue state), obtained from the PR’s base. Use it for:

```bash
git clone <repo_url> <folder>
cd <folder>
git checkout <base_sha>
```

Then follow the PR Writer workflow: Dockerfile, README, pin dependencies, run tests, then first interaction.

## Workflow reminder (PR Writer)

1. **Freeze** the issue in Revelo before any work
2. Clone repo and checkout `base_sha`
3. Edit only **Dockerfile** and **dependencies/requirements** to get builds and tests passing
4. Add README instructions for Docker and tests
5. Pin dependencies, then run tests
6. Start HFI with the issue title + description as the first prompt

## Reference

- [Pre-selected issues spreadsheet](https://docs.google.com/spreadsheets/d/1WnGf8ULFHVpTjnpLz46DH-UrvOCLtkDmqilZLzaS4KM/edit?gid=1184439532)
- [Base SHA fetcher spreadsheet](https://docs.google.com/spreadsheets/d/1f3SdoVoBruHO7KoEqBFVf7QwT1G7sSdAHuIN6ixOaeQ/edit?gid=0)

## License

Internal use for PR Writer HFI project.
