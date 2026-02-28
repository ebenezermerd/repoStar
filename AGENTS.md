# AGENTS.md

## Cursor Cloud specific instructions

**Issue Finder** is a Python CLI tool that searches GitHub for issues matching PR Writer HFI project criteria. See `README.md` for full usage and CLI options.

### Running the tool

```bash
pip install -r requirements.txt
python -m issue_finder --help
```

The tool requires a GitHub token with sufficient rate limits. The `GITHUB_TOKEN` env var may not carry valid credentials in cloud environments; use `$(gh auth token)` instead:

```bash
python -m issue_finder --token "$(gh auth token)" --repo owner/repo
```

### Lint and type checking

No linter config ships with the repo. Use `ruff` and `pyright` (install separately):

```bash
ruff check issue_finder/
pyright issue_finder/
```

Pre-existing lint/type issues exist in the codebase (unused imports, type annotation on `get_repo` return).

### Key caveats

- **Rate limits**: The tool makes many GitHub API calls per issue (fetching all closed PRs to find linked ones). Even with an authenticated token (~5k/hr), scanning large repos can exhaust limits quickly. Use `--max-repos` and `--max-issues-per-repo` to constrain.
- **No automated tests**: The repo has no test suite. Verification is done by running the CLI.
- **No setup.py/pyproject.toml**: The package is run directly via `python -m issue_finder`, not installed as a distribution.
