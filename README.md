# repoStar - GitHub Issue Finder for PR Writer Project

A Python tool that finds GitHub issues precisely matching the criteria for the PR Writer AI training annotation project. It searches repositories, analyzes closed issues, evaluates linked PRs, and scores complexity to find ideal candidates for AI code review training.

## Criteria

The tool filters issues based on these requirements:

| Criteria | Requirement |
|---|---|
| Language | Python |
| Repository Stars | >= 200 |
| Repository Size | < 200 MB |
| Issue State | Closed |
| Issue Description | Pure text only (no images, no links) |
| Issue Description | Non-trivial length (>= 30 chars) |
| Linked PR | Exactly one merged PR |
| Python Code Files Changed | >= 4 (excluding test/doc files) |
| Substantial Changes | At least one file with >= 20 line changes |

## Installation

```bash
pip install -r requirements.txt
```

Requires Python 3.10+.

## Authentication

The tool needs a GitHub API token. It tries these sources in order:

1. `--token` CLI argument
2. `GITHUB_TOKEN` environment variable
3. `gh auth token` (GitHub CLI, if installed)

```bash
export GITHUB_TOKEN="ghp_your_token_here"
```

## Usage

### Analyze a specific issue

```bash
python -m issue_finder analyze <owner> <repo> <issue_number>

# Example:
python -m issue_finder analyze pallets flask 5000
```

### Analyze an issue by URL

```bash
python -m issue_finder analyze-url https://github.com/pallets/flask/issues/5000
```

### Scan a specific repository for matching issues

```bash
python -m issue_finder repo <owner> <repo> [OPTIONS]

# Example:
python -m issue_finder repo psf requests --max-issues 30 --passing-only
```

### Search GitHub for repos and find matching issues

```bash
python -m issue_finder search [OPTIONS]

# Example with custom query:
python -m issue_finder search --query "language:Python stars:>=500" --max-repos 5

# Example with complexity filter:
python -m issue_finder search --min-complexity 30 --max-repos 10
```

### Batch analyze issues from a file

Create a text file with one issue per line:

```
pallets/flask#5000
django/django#30000
psf/requests#6200
```

Then run:

```bash
python -m issue_finder batch-analyze issues.txt --export-json results.json
```

### Check API rate limit

```bash
python -m issue_finder ratelimit
```

## Options

All analysis commands support these options:

| Option | Default | Description |
|---|---|---|
| `--min-stars` | 200 | Minimum repository stars |
| `--max-size` | 200.0 | Maximum repository size in MB |
| `--min-py-files` | 4 | Minimum Python code files changed (excluding test/doc) |
| `--min-substantial` | 20 | Minimum line changes for a file to count as substantial |
| `-v` / `--verbose` | off | Enable debug logging |

Additional options for `search` and `repo` commands:

| Option | Default | Description |
|---|---|---|
| `--max-repos` | 10 | Maximum repositories to search |
| `--max-issues` | 20/50 | Maximum issues to analyze per repo |
| `--min-complexity` | 25.0/0.0 | Minimum complexity score |
| `--passing-only` | off | Show only issues meeting all criteria |
| `--export-json` | none | Export results to JSON file |
| `--export-csv` | none | Export results to CSV file |

## Complexity Scoring

The tool assigns a complexity score (0-100) based on:

- Number of Python code files changed (up to 30 points)
- Total lines of code changed (up to 20 points)
- Largest single file change size (up to 10 points)
- Issue description length/detail (up to 10 points)
- Number of distinct directories touched (up to 15 points)
- Presence of test file changes (5 points)

Higher scores indicate more complex issues that are better candidates for the PR Writer project.

## Output

Results are displayed as rich terminal tables showing:

- Repository info (stars, size)
- Issue details (title, purity, linked PR)
- File change breakdown (Python code vs test vs doc)
- Complexity score with color coding (green >= 50, yellow >= 30, red < 30)
- Pass/fail status against all criteria
- Detailed rejection reasons for failing issues

Results can be exported to JSON or CSV for further analysis.
