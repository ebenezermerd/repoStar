#!/usr/bin/env python3
"""Issue Finder CLI - Find best GitHub issues for PR Writer HFI project."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn

from .config import GITHUB_SEARCH_EXCLUSIONS
from .github_client import GitHubClient
from .issue_analyzer import IssueAnalyzer, pre_filter
from .repo_analyzer import analyze_repo
from .profiles import load_profile, PR_WRITER_PROFILE

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


def _result_row(repo_info, analysis) -> dict:
    """Build a result row dict from repo info and analysis."""
    base_sha = ""
    if analysis.pr_analysis and analysis.pr_analysis.base_sha:
        base_sha = analysis.pr_analysis.base_sha
    return {
        "repo": repo_info.full_name,
        "repo_url": repo_info.html_url,
        "stars": repo_info.stars,
        "size_mb": round(repo_info.size_kb / 1024, 2),
        "issue_number": analysis.issue.number,
        "issue_url": analysis.issue.html_url,
        "issue_title": analysis.issue.title,
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


# ── Legacy sync search (kept for backward compatibility) ─────


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
    """Search and analyze repositories and issues (sync, legacy)."""
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
                results.append(_result_row(repo_info, analysis))

                if len(results) >= 50:
                    break

            if len(results) >= 50:
                break

    return results


# ── Async search (new) ───────────────────────────────────────


async def run_async_search(
    token: str | None = None,
    profile_name: str = "pr_writer",
    max_repos: int = 50,
    max_issues_per_repo: int = 50,
    concurrency: int = 10,
    use_cache: bool = True,
    discover: bool = False,
    search_query: str | None = None,
) -> list[dict]:
    """Search and analyze repos using async client — much faster."""
    from .async_client import AsyncGitHubClient
    from .cache import CacheStore

    import os
    token = token or os.environ.get("GITHUB_TOKEN")

    if not token and discover:
        console.print(
            "[yellow]Warning: No token set. Discovery uses many API calls and will be slow/rate-limited.[/yellow]\n"
            "[dim]  Use --token <token> or set GITHUB_TOKEN env var.[/dim]"
        )

    profile = load_profile(profile_name)
    cache = CacheStore(enabled=use_cache)
    client = AsyncGitHubClient(token=token, cache=cache, concurrency=concurrency)

    try:
        if discover:
            from .discovery import DiscoveryEngine
            engine = DiscoveryEngine(client, profile)
            console.print("[cyan]Discovering repos (trending + topics + curated)...[/cyan]")
            repos = await engine.discover(max_repos=max_repos)
            console.print(f"[green]Found {len(repos)} repos to scan[/green]")
        elif search_query:
            console.print(f"[cyan]Searching for '{search_query}'...[/cyan]")
            repos = await client.search_repos(
                query=search_query,
                min_stars=profile.min_stars,
                max_results=max_repos,
            )
        else:
            console.print("[yellow]No search query or --discover flag. Use --discover for auto-discovery.[/yellow]")
            return []

        if not repos:
            console.print("[yellow]No repos found.[/yellow]")
            return []

        # Scan all repos in parallel
        scanned = 0

        def on_repo_done(repo, results):
            nonlocal scanned
            scanned += 1
            status = f"[green]{len(results)} hits[/green]" if results else "[dim]0[/dim]"
            console.print(f"  [{scanned}/{len(repos)}] {repo.full_name} — {status}")

        console.print(f"[cyan]Scanning {len(repos)} repos (concurrency={concurrency})...[/cyan]")
        all_results = await client.scan_repos_parallel(
            repos, profile,
            max_issues_per_repo=max_issues_per_repo,
            pre_filter=pre_filter,
            on_repo_done=on_repo_done,
        )

        # Convert to result dicts
        rows = []
        for r in all_results:
            repo_info = await client.get_repo_info(r.issue.html_url.split("/issues/")[0].split("github.com/")[-1])
            if not repo_info:
                # Minimal fallback
                from .github_client import RepoInfo
                repo_name = r.issue.html_url.split("/issues/")[0].split("github.com/")[-1]
                repo_info = RepoInfo(
                    full_name=repo_name, stars=0, size_kb=0, language="Python",
                    default_branch="", html_url=f"https://github.com/{repo_name}",
                    description=None, pushed_at=None,
                )
            rows.append(_result_row(repo_info, r))

        cache_stats = cache.stats()
        console.print(f"[dim]Cache: {cache_stats['hits']} hits, {cache_stats['misses']} misses ({cache_stats['hit_rate']})[/dim]")

        return rows
    finally:
        await client.close()


# ── Output ───────────────────────────────────────────────────


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


# ── CLI entry point ──────────────────────────────────────────


def main() -> int:
    """CLI entry point."""
    try:
        return _main_inner()
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        return 130


def _main_inner() -> int:
    parser = argparse.ArgumentParser(
        description="Find GitHub issues that fit PR Writer HFI project criteria."
    )
    parser.add_argument(
        "--token", default=None,
        help="GitHub token (or set GITHUB_TOKEN). Higher rate limits with token.",
    )
    parser.add_argument(
        "--min-stars", type=int, default=200,
        help="Minimum repository stars (default: 200)",
    )
    parser.add_argument(
        "--max-repos", type=int, default=50,
        help="Max repos to scan (default: 50)",
    )
    parser.add_argument(
        "--max-issues-per-repo", type=int, default=100,
        help="Max closed issues per repo (default: 100)",
    )
    parser.add_argument(
        "--excluded", type=str, default=None,
        help="File with excluded issue URLs or repo#number (one per line)",
    )
    parser.add_argument(
        "--min-score", type=float, default=5.0,
        help="Minimum analysis score to include (default: 5.0)",
    )
    parser.add_argument(
        "--json", type=str, default=None,
        help="Output results to JSON file",
    )
    parser.add_argument(
        "--csv", type=str, default=None,
        help="Output results to CSV file",
    )
    parser.add_argument(
        "--repo", type=str, default=None,
        help="Analyze single repository (owner/repo) instead of searching",
    )
    parser.add_argument(
        "-i", "--interactive", action="store_true", default=False,
        help="Launch interactive mode (browse repos, pick issues, analyze live)",
    )
    # New async flags
    parser.add_argument(
        "--discover", action="store_true", default=False,
        help="Auto-discover repos (trending + topics + curated) — no search keyword needed",
    )
    parser.add_argument(
        "--profile", type=str, default="pr_writer",
        help="Scoring profile: pr_writer (default), general, or path to JSON profile",
    )
    parser.add_argument(
        "--no-cache", action="store_true", default=False,
        help="Disable disk cache",
    )
    parser.add_argument(
        "--concurrency", type=int, default=None,
        help="Max parallel requests (default: 10 with token, 2 without)",
    )
    parser.add_argument(
        "--search", type=str, default=None,
        help="Async search query (uses new fast engine)",
    )

    args = parser.parse_args()

    # Resolve token: CLI arg > env var > saved file
    if not args.token:
        import os
        args.token = os.environ.get("GITHUB_TOKEN")
    if not args.token:
        from .interactive import _load_saved_token
        args.token = _load_saved_token()

    if args.interactive:
        from .interactive import run_interactive
        return run_interactive(token=args.token)

    # Async paths: --discover or --search
    if args.discover or args.search:
        results = asyncio.run(run_async_search(
            token=args.token,
            profile_name=args.profile,
            max_repos=args.max_repos,
            max_issues_per_repo=args.max_issues_per_repo,
            concurrency=args.concurrency,
            use_cache=not args.no_cache,
            discover=args.discover,
            search_query=args.search,
        ))
        print_results(results)
        _save_outputs(results, args)
        return 0

    if args.repo:
        # Single repo mode (sync)
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
                results.append(_result_row(repo_info, analysis))
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
    _save_outputs(results, args)
    return 0


def _save_outputs(results: list[dict], args) -> None:
    """Save results to JSON/CSV if requested."""
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


if __name__ == "__main__":
    sys.exit(main())
