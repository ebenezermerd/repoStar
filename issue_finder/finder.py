from __future__ import annotations

import json
from dataclasses import asdict

from .github_client import GitHubApiError, GitHubClient
from .github_queries import get_single_closing_pr
from .path_rules import is_non_test_non_doc_python
from .scoring import compute_change_stats, score_candidate
from .text_rules import looks_like_pure_text
from .types import Candidate, RepoRef


def build_repo_search_query(*, min_stars: int, extra_query: str | None, exclude_terms: list[str]) -> str:
    base = f"language:Python stars:>={min_stars} archived:false fork:false"
    if extra_query:
        base = f"{base} {extra_query}".strip()
    for term in exclude_terms:
        term = term.strip()
        if term:
            base = f"{base} NOT {term}"
    return base


def find_candidates(
    *,
    gh: GitHubClient,
    repo_query: str,
    max_repos: int,
    max_repo_size_mb: float,
    max_issues_per_repo: int,
    min_non_test_doc_py_files: int,
    min_max_file_changes: int,
    require_pure_text: bool,
    reject_screenshot_mentions: bool,
    require_merged_pr: bool,
    max_files_per_pr: int,
    max_candidates: int,
    verbose: bool,
) -> list[Candidate]:
    repos = gh.search_repositories(query=repo_query, max_repos=max_repos)
    candidates: list[Candidate] = []

    for r in repos:
        if len(candidates) >= max_candidates:
            break

        full_name = r.get("full_name")
        if not isinstance(full_name, str) or "/" not in full_name:
            continue
        owner, name = full_name.split("/", 1)
        repo = RepoRef(owner=owner, name=name)

        stars = int(r.get("stargazers_count") or 0)
        size_kb = float(r.get("size") or 0.0)
        size_mb = size_kb / 1024.0
        repo_url = r.get("html_url") or f"https://github.com/{repo.full_name}"

        if size_mb > max_repo_size_mb:
            continue

        try:
            issues = gh.search_closed_issues(repo=repo, max_issues=max_issues_per_repo)
        except GitHubApiError:
            continue

        for i in issues:
            if len(candidates) >= max_candidates:
                break
            issue_number = i.get("number")
            if not isinstance(issue_number, int):
                continue

            try:
                issue = gh.get_issue(repo=repo, number=issue_number)
            except GitHubApiError:
                continue

            # Exclude pull requests masquerading in /issues/.
            if issue.get("pull_request") is not None:
                continue
            if (issue.get("state") or "").lower() != "closed":
                continue

            title = issue.get("title") or ""
            body = issue.get("body") or ""
            if not isinstance(title, str) or not isinstance(body, str):
                continue

            text_reasons: tuple[str, ...] = tuple()
            if require_pure_text:
                ok_text, text_reasons = looks_like_pure_text(
                    title,
                    body,
                    reject_screenshot_mentions=reject_screenshot_mentions,
                )
                if not ok_text:
                    continue

            closing_pr = None
            try:
                closing_pr = get_single_closing_pr(gh=gh, repo=repo, issue_number=issue_number)
            except GitHubApiError:
                continue
            if closing_pr is None:
                continue

            try:
                pr = gh.get_pull(repo=repo, number=closing_pr.number)
            except GitHubApiError:
                continue

            if require_merged_pr and pr.get("merged_at") is None:
                # Require merged PRs to make "before/after" stable.
                continue

            base_sha = (((pr.get("base") or {}).get("sha")) or "").strip()
            if not base_sha:
                continue
            merge_commit_sha = (pr.get("merge_commit_sha") or None)  # may be null for squash/rebase edge cases

            try:
                pr_files = gh.list_pull_files(repo=repo, number=closing_pr.number, max_files=max_files_per_pr)
            except GitHubApiError:
                continue

            stats = compute_change_stats(pr_files=pr_files, is_non_test_non_doc_python=is_non_test_non_doc_python)

            reasons: list[str] = list(text_reasons)

            if stats.changed_non_test_doc_py_files < min_non_test_doc_py_files:
                continue
            if stats.max_file_changes_non_test_doc_py < min_max_file_changes:
                continue

            score = score_candidate(
                stars=stars,
                size_mb=size_mb,
                issue_body_len=len(body),
                stats=stats,
            )

            if verbose:
                reasons.append("accepted")

            candidates.append(
                Candidate(
                    repo_full_name=repo.full_name,
                    repo_url=str(repo_url),
                    repo_stars=stars,
                    repo_size_mb=round(size_mb, 2),
                    issue_number=issue_number,
                    issue_title=title.strip(),
                    issue_url=str(issue.get("html_url") or f"https://github.com/{repo.full_name}/issues/{issue_number}"),
                    issue_body_len=len(body),
                    pr_number=int(pr.get("number") or closing_pr.number),
                    pr_url=str(pr.get("html_url") or closing_pr.url),
                    base_sha=base_sha,
                    merge_commit_sha=str(merge_commit_sha) if merge_commit_sha else None,
                    changed_py_files=stats.changed_py_files,
                    changed_non_test_doc_py_files=stats.changed_non_test_doc_py_files,
                    total_changes_non_test_doc_py=stats.total_changes_non_test_doc_py,
                    max_file_changes_non_test_doc_py=stats.max_file_changes_non_test_doc_py,
                    top_changed_files=stats.top_changed_files,
                    score=round(float(score), 3),
                    reasons=tuple(reasons),
                )
            )

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def candidates_to_json(candidates: list[Candidate]) -> str:
    return json.dumps([asdict(c) for c in candidates], indent=2, sort_keys=True)

