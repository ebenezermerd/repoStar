"""Command-line interface for the GitHub Issue Finder."""

import sys
import logging

import click
from rich.console import Console

from .github_api import GitHubClient, GitHubAPIError
from .analyzer import IssueAnalyzer
from .display import (
    display_issue_detail,
    display_results_table,
    export_results_json,
    export_results_csv,
    console,
)

logger = logging.getLogger("issue_finder")


def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root_logger = logging.getLogger("issue_finder")
    root_logger.setLevel(level)
    root_logger.addHandler(handler)


@click.group()
@click.option("--token", envvar="GITHUB_TOKEN", help="GitHub API token")
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose output")
@click.pass_context
def cli(ctx, token, verbose):
    """GitHub Issue Finder for PR Writer Project.

    Finds GitHub issues matching specific criteria:
    - Python repos with 200+ stars, <200MB
    - Closed issues with pure text descriptions
    - Linked to exactly one merged PR
    - PR changes at least 4 non-test/non-doc Python files
    - At least one file with substantial changes
    """
    setup_logging(verbose)
    ctx.ensure_object(dict)
    try:
        ctx.obj["client"] = GitHubClient(token=token)
    except GitHubAPIError as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


@cli.command()
@click.argument("owner")
@click.argument("repo")
@click.argument("issue_number", type=int)
@click.option("--min-stars", default=200, help="Minimum stars requirement")
@click.option("--max-size", default=200.0, help="Maximum repo size in MB")
@click.option("--min-py-files", default=4, help="Minimum Python code files changed")
@click.option("--min-substantial", default=20, help="Minimum line changes for substantial file")
@click.pass_context
def analyze(ctx, owner, repo, issue_number, min_stars, max_size, min_py_files, min_substantial):
    """Analyze a specific issue in detail.

    Example: issue-finder analyze python django 12345
    """
    client = ctx.obj["client"]
    analyzer = IssueAnalyzer(
        client, min_stars=min_stars, max_size_mb=max_size,
        min_python_files=min_py_files, min_substantial=min_substantial,
    )

    console.print(f"\nAnalyzing [cyan]{owner}/{repo}[/cyan] issue [cyan]#{issue_number}[/cyan]...")
    analysis = analyzer.analyze_specific_issue(owner, repo, issue_number)
    display_issue_detail(analysis)


@cli.command()
@click.argument("owner")
@click.argument("repo")
@click.option("--max-issues", default=50, help="Maximum issues to analyze")
@click.option("--min-complexity", default=0.0, help="Minimum complexity score to include")
@click.option("--min-stars", default=200, help="Minimum stars requirement")
@click.option("--max-size", default=200.0, help="Maximum repo size in MB")
@click.option("--min-py-files", default=4, help="Minimum Python code files changed")
@click.option("--min-substantial", default=20, help="Minimum line changes for substantial file")
@click.option("--passing-only", is_flag=True, help="Show only issues that meet all criteria")
@click.option("--export-json", type=str, help="Export results to JSON file")
@click.option("--export-csv", type=str, help="Export results to CSV file")
@click.pass_context
def repo(ctx, owner, repo, max_issues, min_complexity, min_stars, max_size,
         min_py_files, min_substantial, passing_only, export_json, export_csv):
    """Analyze closed issues from a specific repository.

    Example: issue-finder repo pallets flask --max-issues 30
    """
    client = ctx.obj["client"]
    analyzer = IssueAnalyzer(
        client, min_stars=min_stars, max_size_mb=max_size,
        min_python_files=min_py_files, min_substantial=min_substantial,
    )

    console.print(f"\nAnalyzing issues from [cyan]{owner}/{repo}[/cyan]...")
    results = analyzer.analyze_specific_repo(owner, repo, max_issues=max_issues)

    if passing_only:
        results = [r for r in results if r.meets_criteria]
    if min_complexity > 0:
        results = [r for r in results if r.complexity_score >= min_complexity]

    display_results_table(results, title=f"Issues from {owner}/{repo}")

    for r in results:
        if r.meets_criteria:
            display_issue_detail(r)

    if export_json:
        export_results_json(results, export_json)
    if export_csv:
        export_results_csv(results, export_csv)


@cli.command()
@click.option("--query", default=None, help="Custom GitHub search query")
@click.option("--max-repos", default=10, help="Maximum repositories to search")
@click.option("--max-issues", default=20, help="Maximum issues per repo")
@click.option("--min-complexity", default=25.0, help="Minimum complexity score")
@click.option("--min-stars", default=200, help="Minimum stars requirement")
@click.option("--max-size", default=200.0, help="Maximum repo size in MB")
@click.option("--min-py-files", default=4, help="Minimum Python code files changed")
@click.option("--min-substantial", default=20, help="Minimum line changes for substantial file")
@click.option("--export-json", type=str, help="Export results to JSON file")
@click.option("--export-csv", type=str, help="Export results to CSV file")
@click.pass_context
def search(ctx, query, max_repos, max_issues, min_complexity, min_stars, max_size,
           min_py_files, min_substantial, export_json, export_csv):
    """Search GitHub for repos and find matching issues.

    Example: issue-finder search --max-repos 5 --min-complexity 30
    """
    client = ctx.obj["client"]
    analyzer = IssueAnalyzer(
        client, min_stars=min_stars, max_size_mb=max_size,
        min_python_files=min_py_files, min_substantial=min_substantial,
    )

    console.print("\nSearching for matching issues across GitHub...")
    if query:
        console.print(f"Query: [cyan]{query}[/cyan]")

    results = analyzer.search_and_analyze(
        query=query, max_repos=max_repos,
        max_issues_per_repo=max_issues, min_complexity=min_complexity,
    )

    display_results_table(results, title="Search Results - Matching Issues")

    for r in results[:10]:
        display_issue_detail(r)

    if export_json:
        export_results_json(results, export_json)
    if export_csv:
        export_results_csv(results, export_csv)


@cli.command()
@click.pass_context
def ratelimit(ctx):
    """Check GitHub API rate limit status."""
    client = ctx.obj["client"]
    remaining, limit = client.check_rate_limit()
    color = "green" if remaining > 500 else "yellow" if remaining > 100 else "red"
    console.print(f"\nGitHub API Rate Limit: [{color}]{remaining}[/{color}] / {limit}")


@cli.command(name="batch-analyze")
@click.argument("issues_file", type=click.Path(exists=True))
@click.option("--min-stars", default=200, help="Minimum stars requirement")
@click.option("--max-size", default=200.0, help="Maximum repo size in MB")
@click.option("--min-py-files", default=4, help="Minimum Python code files changed")
@click.option("--min-substantial", default=20, help="Minimum line changes for substantial file")
@click.option("--export-json", type=str, help="Export results to JSON file")
@click.option("--export-csv", type=str, help="Export results to CSV file")
@click.pass_context
def batch_analyze(ctx, issues_file, min_stars, max_size, min_py_files, min_substantial,
                  export_json, export_csv):
    """Analyze a batch of issues from a file.

    The file should contain one issue per line in the format:
    owner/repo#issue_number

    Example file contents:
        pallets/flask#5000
        django/django#30000
    """
    client = ctx.obj["client"]
    analyzer = IssueAnalyzer(
        client, min_stars=min_stars, max_size_mb=max_size,
        min_python_files=min_py_files, min_substantial=min_substantial,
    )

    with open(issues_file, "r") as f:
        lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    results = []
    for line in lines:
        try:
            repo_part, issue_num = line.split("#")
            parts = repo_part.strip().rstrip("/").split("/")
            owner = parts[-2]
            repo_name = parts[-1]
            issue_number = int(issue_num.strip())
        except (ValueError, IndexError):
            console.print(f"[yellow]Skipping invalid line: {line}[/yellow]")
            continue

        console.print(f"Analyzing [cyan]{owner}/{repo_name}#{issue_number}[/cyan]...")
        analysis = analyzer.analyze_specific_issue(owner, repo_name, issue_number)
        results.append(analysis)

    display_results_table(results, title="Batch Analysis Results")

    for r in results:
        if r.meets_criteria:
            display_issue_detail(r)

    if export_json:
        export_results_json(results, export_json)
    if export_csv:
        export_results_csv(results, export_csv)


@cli.command(name="analyze-url")
@click.argument("url")
@click.option("--min-stars", default=200, help="Minimum stars requirement")
@click.option("--max-size", default=200.0, help="Maximum repo size in MB")
@click.option("--min-py-files", default=4, help="Minimum Python code files changed")
@click.option("--min-substantial", default=20, help="Minimum line changes for substantial file")
@click.pass_context
def analyze_url(ctx, url, min_stars, max_size, min_py_files, min_substantial):
    """Analyze an issue from its GitHub URL.

    Example: issue-finder analyze-url https://github.com/pallets/flask/issues/5000
    """
    import re
    match = re.match(r'https?://github\.com/([^/]+)/([^/]+)/issues/(\d+)', url)
    if not match:
        console.print("[red]Invalid GitHub issue URL format.[/red]")
        console.print("Expected: https://github.com/owner/repo/issues/NUMBER")
        sys.exit(1)

    owner, repo_name, issue_number = match.group(1), match.group(2), int(match.group(3))

    client = ctx.obj["client"]
    analyzer = IssueAnalyzer(
        client, min_stars=min_stars, max_size_mb=max_size,
        min_python_files=min_py_files, min_substantial=min_substantial,
    )

    console.print(f"\nAnalyzing [cyan]{url}[/cyan]...")
    analysis = analyzer.analyze_specific_issue(owner, repo_name, issue_number)
    display_issue_detail(analysis)


def main():
    cli(obj={})


if __name__ == "__main__":
    main()
