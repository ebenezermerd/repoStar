from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .finder import build_repo_search_query, find_candidates
from .github_client import GitHubApiError, GitHubClient
from .output import write_csv, write_json


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="issue-finder",
        description="Find closed GitHub issues suitable for PR-writer style tasks.",
    )
    p.add_argument(
        "--github-token",
        default=None,
        help="GitHub token (overrides GITHUB_TOKEN/GH_TOKEN env var).",
    )
    p.add_argument("--min-stars", type=int, default=200)
    p.add_argument(
        "--repo-query",
        default=None,
        help="If provided, uses this exact GitHub repo search query (overrides --min-stars/--extra-repo-query/--exclude-term).",
    )
    p.add_argument(
        "--extra-repo-query",
        default="NOT collection NOT list NOT guide NOT projects NOT exercises",
        help="Extra GitHub repository search terms appended to the base query.",
    )
    p.add_argument(
        "--exclude-term",
        action="append",
        default=[],
        help="Additional term to exclude from repo search (adds `NOT <term>`).",
    )
    p.add_argument("--max-repos", type=int, default=50)
    p.add_argument("--max-repo-size-mb", type=float, default=200.0)
    p.add_argument("--max-issues-per-repo", type=int, default=30)
    p.add_argument("--max-candidates", type=int, default=50)
    p.add_argument("--min-non-test-doc-py-files", type=int, default=4)
    p.add_argument(
        "--min-max-file-changes",
        type=int,
        default=60,
        help="Require at least one non-test/non-doc Python file with >= this many changes.",
    )
    p.add_argument(
        "--require-pure-text",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="If enabled, rejects issues containing URLs/markdown links/images (strict heuristic).",
    )
    p.add_argument(
        "--reject-screenshot-mentions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="If enabled, rejects issues whose text mentions 'screenshot'.",
    )
    p.add_argument(
        "--require-merged-pr",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="If enabled, only accepts merged PRs as closers.",
    )
    p.add_argument("--max-files-per-pr", type=int, default=500)
    p.add_argument("--out-dir", default="out")
    p.add_argument("--format", choices=["csv", "json", "both"], default="both")
    p.add_argument("--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        gh = GitHubClient.from_env(token=args.github_token)
    except GitHubApiError as e:
        print(str(e), file=sys.stderr)
        return 2

    query = (
        args.repo_query
        if args.repo_query
        else build_repo_search_query(
            min_stars=args.min_stars,
            extra_query=args.extra_repo_query,
            exclude_terms=args.exclude_term,
        )
    )

    candidates = find_candidates(
        gh=gh,
        repo_query=query,
        max_repos=args.max_repos,
        max_repo_size_mb=args.max_repo_size_mb,
        max_issues_per_repo=args.max_issues_per_repo,
        min_non_test_doc_py_files=args.min_non_test_doc_py_files,
        min_max_file_changes=args.min_max_file_changes,
        require_pure_text=args.require_pure_text,
        reject_screenshot_mentions=args.reject_screenshot_mentions,
        require_merged_pr=args.require_merged_pr,
        max_files_per_pr=args.max_files_per_pr,
        max_candidates=args.max_candidates,
        verbose=args.verbose,
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.format in ("json", "both"):
        write_json(out_dir / "candidates.json", candidates)
    if args.format in ("csv", "both"):
        write_csv(out_dir / "candidates.csv", candidates)

    print(f"Wrote {len(candidates)} candidates to {out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

