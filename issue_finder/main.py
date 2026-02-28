#!/usr/bin/env python3
"""Issue Finder CLI - Find best GitHub issues for PR Writer HFI project."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn

from .config import GITHUB_SEARCH_EXCLUSIONS
from .github_client import GitHubClient
from .issue_analyzer import IssueAnalyzer
from .repo_analyzer import analyze_repo

console = Console()


def _normalize_excluded(line: str) -> str | None:
    """Normalize URL or repo#n to owner/repo#n format."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "github.com" in line and "/issues/" in line:
        parts = line.rstrip("/").split("/")
        if len(parts) >= 5:
            owner, repo = parts[-4], parts[-3]
            num = parts[-1]
            return f"{owner}/{repo}#{num}"
    if "#" in line and "/" in line:
        return line
    return None


def load_excluded_issues(path: str | None) -> set[str]:
    """Load excluded issue URLs or repo#issue from file (one per line)."""
    if not path or not Path(path).exists():
        return set()
    excluded = set()
    with open(path) as f:
        for line in f:
            norm = _normalize_excluded(line)
            if norm:
                excluded.add(norm)
            elif line.strip() and not line.strip().startswith("#"):
                excluded.add(line.strip())
    return excluded


def issue_key(repo: str, issue_num: int) -> str:
    """Generate key for deduplication."""
    return f"{repo}#{issue_num}"


def run_search(
    token: str | None = None,
    min_stars: int = 200,
    max_repos: int = 50,
    max_issues_per_repo: int = 100,
    excluded_file: str | None = None,
    output_json: str | None = None,
    output_csv: str | None = None,
    min_score: float = 5.0,
) -> list[dict]:
    """Search and analyze repositories and issues."""
    client = GitHubClient(token)
    analyzer = IssueAnalyzer(client)
    excluded = load_excluded_issues(excluded_file)

    results = []
    seen = set()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task_repos = progress.add_task("Searching Python repositories...", total=None)
        task_issues = progress.add_task("Analyzing issues...", total=None)

        for repo_info in client.search_python_repos(
            min_stars=min_stars, exclude_words=GITHUB_SEARCH_EXCLUSIONS, max_results=max_repos
        ):
            progress.update(task_repos, description=f"Repo: {repo_info.full_name} ({repo_info.stars} stars)")

            repo_result = analyze_repo(repo_info)
            if not repo_result.passes:
                continue

            if repo_info.size_kb > 200 * 1024:
                continue

            for issue in client.get_closed_issues(
                repo_info.full_name, state="closed", max_issues=max_issues_per_repo
            ):
                key = issue_key(repo_info.full_name, issue.number)
                if key in seen or key in excluded:
                    continue

                progress.update(task_issues, description=f"Issue: {repo_info.full_name}#{issue.number}")

                analysis = analyzer.analyze_issue(repo_info.full_name, issue)

                if analysis.score < min_score or not analysis.passes:
                    continue

                seen.add(key)

                base_sha = ""
                if analysis.pr_analysis and analysis.pr_analysis.base_sha:
                    base_sha = analysis.pr_analysis.base_sha

                row = {
                    "repo": repo_info.full_name,
                    "repo_url": repo_info.html_url,
                    "stars": repo_info.stars,
                    "size_mb": round(repo_info.size_kb / 1024, 2),
                    "issue_number": issue.number,
                    "issue_url": issue.html_url,
                    "issue_title": issue.title,
                    "pr_url": analysis.pr_analysis.html_url if analysis.pr_analysis else "",
                    "pr_number": analysis.pr_analysis.number if analysis.pr_analysis else 0,
                    "score": round(analysis.score, 2),
                    "code_files_changed": analysis.details.get("code_python_files_changed", 0),
                    "total_additions": analysis.details.get("total_additions", 0),
                    "total_deletions": analysis.details.get("total_deletions", 0),
                    "complexity_hint": analysis.complexity_hint,
                    "reasons": analysis.reasons,
                    "base_sha": base_sha,
                }
                results.append(row)

                if len(results) >= 50:
                    break

            if len(results) >= 50:
                break

    return results


def print_results(results: list[dict]) -> None:
    """Print results to console as a rich table."""
    if not results:
        console.print("[yellow]No matching issues found. Try relaxing --min-score or --min-stars.[/yellow]")
        return

    table = Table(title="PR Writer Issue Finder - Best Matches", show_lines=False)
    table.add_column("Repo", style="cyan")
    table.add_column("Stars", justify="right")
    table.add_column("Issue", style="green")
    table.add_column("Score", justify="right")
    table.add_column("Files", justify="right")
    table.add_column("Complexity")
    table.add_column("URL")

    for r in sorted(results, key=lambda x: (-x["score"], -x["stars"])):
        table.add_row(
            r["repo"],
            str(r["stars"]),
            f"#{r['issue_number']}: {r['issue_title'][:40]}...",
            str(r["score"]),
            str(r["code_files_changed"]),
            r["complexity_hint"][:20],
            r["issue_url"],
        )
    console.print(table)
    console.print(f"\n[green]Found {len(results)} matching issues.[/green]")


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Find GitHub issues that fit PR Writer HFI project criteria."
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub token (or set GITHUB_TOKEN). Higher rate limits with token.",
    )
    parser.add_argument(
        "--min-stars",
        type=int,
        default=200,
        help="Minimum repository stars (default: 200)",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=50,
        help="Max repos to scan (default: 50)",
    )
    parser.add_argument(
        "--max-issues-per-repo",
        type=int,
        default=100,
        help="Max closed issues per repo (default: 100)",
    )
    parser.add_argument(
        "--excluded",
        type=str,
        default=None,
        help="File with excluded issue URLs or repo#number (one per line)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=5.0,
        help="Minimum analysis score to include (default: 5.0)",
    )
    parser.add_argument(
        "--json",
        type=str,
        default=None,
        help="Output results to JSON file",
    )
    parser.add_argument(
        "--csv",
        type=str,
        default=None,
        help="Output results to CSV file",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Analyze single repository (owner/repo) instead of searching",
    )

    args = parser.parse_args()

    if args.repo:
        # Single repo mode
        client = GitHubClient(args.token)
        analyzer = IssueAnalyzer(client)
        repo_info = client.get_repo_info(args.repo)
        if not repo_info:
            console.print(f"[red]Repository not found: {args.repo}[/red]")
            return 1
        repo_result = analyze_repo(repo_info)
        if not repo_result.passes:
            console.print(f"[red]Repo does not meet criteria: {repo_result.summary}[/red]")
            return 1
        console.print(f"[green]Scanning issues in {args.repo}...[/green]")
        results = []
        excluded = load_excluded_issues(args.excluded)
        for issue in client.get_closed_issues(args.repo, max_issues=200):
            key = issue_key(args.repo, issue.number)
            if key in excluded:
                continue
            analysis = analyzer.analyze_issue(args.repo, issue)
            if analysis.score >= args.min_score and analysis.passes:
                base_sha = ""
                if analysis.pr_analysis and analysis.pr_analysis.base_sha:
                    base_sha = analysis.pr_analysis.base_sha
                results.append({
                    "repo": args.repo,
                    "repo_url": repo_info.html_url,
                    "stars": repo_info.stars,
                    "size_mb": round(repo_info.size_kb / 1024, 2),
                    "issue_number": issue.number,
                    "issue_url": issue.html_url,
                    "issue_title": issue.title,
                    "pr_url": analysis.pr_analysis.html_url if analysis.pr_analysis else "",
                    "pr_number": analysis.pr_analysis.number if analysis.pr_analysis else 0,
                    "score": round(analysis.score, 2),
                    "code_files_changed": analysis.details.get("code_python_files_changed", 0),
                    "total_additions": analysis.details.get("total_additions", 0),
                    "total_deletions": analysis.details.get("total_deletions", 0),
                    "complexity_hint": analysis.complexity_hint,
                    "reasons": analysis.reasons,
                    "base_sha": base_sha,
                })
    else:
        results = run_search(
            token=args.token,
            min_stars=args.min_stars,
            max_repos=args.max_repos,
            max_issues_per_repo=args.max_issues_per_repo,
            excluded_file=args.excluded,
            min_score=args.min_score,
        )

    print_results(results)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)
        console.print(f"[green]Saved to {args.json}[/green]")

    if args.csv:
        import csv
        if results:
            with open(args.csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f,
                    fieldnames=[
                        "repo", "stars", "size_mb", "issue_number", "issue_url",
                        "issue_title", "pr_url", "base_sha", "score", "code_files_changed",
                        "total_additions", "total_deletions", "complexity_hint",
                    ],
                    extrasaction="ignore",
                )
                writer.writeheader()
                writer.writerows(results)
        console.print(f"[green]Saved to {args.csv}[/green]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
