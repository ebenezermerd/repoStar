from __future__ import annotations

import argparse
import json
from pathlib import Path

from .finder import FinderConfig, IssueFinder
from .github_client import GitHubClient, GitHubClientError
from .models import IssueCandidate


def _parse_repositories(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    repositories = [item.strip() for item in raw.split(",") if item.strip()]
    return repositories or None


def _format_candidate_row(rank: int, candidate: IssueCandidate) -> str:
    repo_name = candidate.repository.full_name
    base_sha_short = candidate.pull_request.base_sha[:10]
    return (
        f"{rank:>3}  "
        f"{repo_name:<32.32}  "
        f"#{candidate.issue_number:<6}  "
        f"PR#{candidate.pull_request.number:<7}  "
        f"{candidate.repository.stars:>6}  "
        f"{candidate.repository.size_mb:>7.1f}  "
        f"{len(candidate.python_non_test_files):>7}  "
        f"{candidate.max_python_file_change.changes:>8}  "
        f"{candidate.total_python_changes:>10}  "
        f"{candidate.complexity.score:>6.2f}  "
        f"{base_sha_short:<10}"
    )


def _print_candidates(candidates: list[IssueCandidate]) -> None:
    if not candidates:
        print("No matching issues found with current constraints.")
        return

    print(
        "rk  repo                              issue     pull       stars  size_mb  py_files"
        "  max_file  py_changes  score   base_sha"
    )
    print("-" * 136)
    for rank, candidate in enumerate(candidates, start=1):
        print(_format_candidate_row(rank, candidate))
        print(f"    issue: {candidate.issue_title}")
        print(f"    url:   {candidate.issue_url}")
        print(f"    pr:    {candidate.pull_request.html_url}")
        print(f"    notes: {' | '.join(candidate.notes)}")


def _print_rejection_summary(rejections: dict[str, int]) -> None:
    if not rejections:
        return
    print("\nRejected candidates summary:")
    for key, count in sorted(rejections.items(), key=lambda item: item[1], reverse=True):
        print(f"  - {key}: {count}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Find closed GitHub issues suitable for PR-writer workflows with strict"
            " complexity and linkage constraints."
        )
    )
    parser.add_argument(
        "--query",
        default="language:Python archived:false mirror:false",
        help="Base repository search query. stars filter is injected automatically.",
    )
    parser.add_argument(
        "--repos",
        default=None,
        help="Optional comma-separated owner/repo list to analyze directly.",
    )
    parser.add_argument("--top", type=int, default=10, help="Maximum rows to print.")
    parser.add_argument("--min-stars", type=int, default=200)
    parser.add_argument("--max-size-mb", type=float, default=200.0)
    parser.add_argument("--min-python-files", type=int, default=4)
    parser.add_argument("--min-single-file-changes", type=int, default=35)
    parser.add_argument("--min-issue-body-length", type=int, default=80)
    parser.add_argument("--min-complexity-score", type=float, default=50.0)
    parser.add_argument("--max-repositories", type=int, default=30)
    parser.add_argument("--max-repo-search-pages", type=int, default=2)
    parser.add_argument("--issues-per-repository", type=int, default=50)
    parser.add_argument("--max-issue-pages", type=int, default=2)
    parser.add_argument("--max-timeline-pages", type=int, default=1)
    parser.add_argument("--max-pull-file-pages", type=int, default=4)
    parser.add_argument(
        "--include-forks",
        action="store_true",
        help="Include fork repositories in analysis.",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Optional output path to write full candidate payload as JSON.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=60,
        help="Timeout per gh api call.",
    )
    parser.add_argument(
        "--show-rejections",
        action="store_true",
        help="Print rejection reason counts.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    config = FinderConfig(
        min_stars=args.min_stars,
        max_repo_size_mb=args.max_size_mb,
        min_python_files_changed=args.min_python_files,
        min_single_python_file_changes=args.min_single_file_changes,
        min_issue_body_length=args.min_issue_body_length,
        min_complexity_score=args.min_complexity_score,
        max_repositories=args.max_repositories,
        max_repo_search_pages=args.max_repo_search_pages,
        issues_per_repository=args.issues_per_repository,
        max_issue_pages=args.max_issue_pages,
        max_timeline_pages=args.max_timeline_pages,
        max_pull_file_pages=args.max_pull_file_pages,
        include_forks=args.include_forks,
        search_query=args.query,
    )
    repositories = _parse_repositories(args.repos)

    client = GitHubClient(timeout_seconds=args.timeout_seconds)
    finder = IssueFinder(client, config)
    try:
        candidates = finder.find_candidates(specific_repositories=repositories)
    except (GitHubClientError, ValueError) as error:
        print(f"Error: {error}")
        return 1

    top_candidates = candidates[: max(args.top, 0)]
    _print_candidates(top_candidates)

    if args.json_out:
        output_path = Path(args.json_out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps([candidate.to_dict() for candidate in top_candidates], indent=2),
            encoding="utf-8",
        )
        print(f"\nJSON report written to: {output_path}")

    if args.show_rejections:
        _print_rejection_summary(dict(finder.rejection_counts))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
