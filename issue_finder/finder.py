from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .filters import (
    contains_links_or_images,
    dedupe_preserve_order,
    extract_closing_issue_numbers,
    is_python_non_test_non_doc_file,
)
from .github_client import GitHubClient, GitHubClientError, PaginationConfig
from .models import (
    FileChangeSummary,
    IssueCandidate,
    PullRequestSnapshot,
    RepositorySnapshot,
)
from .scoring import score_complexity

PULL_URL_NUMBER_PATTERN = re.compile(r"/pulls?/(\d+)")


@dataclass(frozen=True)
class FinderConfig:
    min_stars: int = 200
    max_repo_size_mb: float = 200.0
    min_python_files_changed: int = 4
    min_single_python_file_changes: int = 35
    min_issue_body_length: int = 80
    min_complexity_score: float = 50.0
    max_repositories: int = 10
    max_repo_search_pages: int = 1
    issues_per_repository: int = 30
    max_issue_pages: int = 1
    max_timeline_pages: int = 1
    max_pull_file_pages: int = 4
    include_forks: bool = False
    search_query: str = "language:Python archived:false mirror:false"


class IssueFinder:
    def __init__(self, client: GitHubClient, config: FinderConfig) -> None:
        self.client = client
        self.config = config
        self.rejection_counts: dict[str, int] = defaultdict(int)

    def find_candidates(
        self,
        *,
        specific_repositories: list[str] | None = None,
    ) -> list[IssueCandidate]:
        repositories = self._collect_repositories(specific_repositories=specific_repositories)
        candidates: list[IssueCandidate] = []

        for repository in repositories:
            snapshot = self._to_repository_snapshot(repository)
            if not snapshot:
                continue

            closed_issues = self.client.list_closed_issues(
                snapshot.owner,
                snapshot.name,
                max_issues=self.config.issues_per_repository,
                pagination=PaginationConfig(max_pages=self.config.max_issue_pages),
            )
            for issue in closed_issues:
                candidate = self._analyze_issue(snapshot, issue)
                if candidate:
                    candidates.append(candidate)

        return sorted(
            candidates,
            key=lambda item: (
                item.complexity.score,
                item.total_python_changes,
                item.repository.stars,
            ),
            reverse=True,
        )

    def _collect_repositories(
        self,
        *,
        specific_repositories: list[str] | None,
    ) -> list[dict[str, Any]]:
        if specific_repositories:
            repositories: list[dict[str, Any]] = []
            for name in specific_repositories:
                owner, repo = self._split_repository_name(name)
                try:
                    repositories.append(self.client.get_repository(owner, repo))
                except GitHubClientError:
                    self._reject("repository_lookup_failed")
            return repositories

        repositories = self.client.search_repositories(
            self.config.search_query,
            min_stars=self.config.min_stars,
            max_pages=self.config.max_repo_search_pages,
        )
        return repositories[: self.config.max_repositories]

    @staticmethod
    def _split_repository_name(name: str) -> tuple[str, str]:
        if "/" not in name:
            raise ValueError(f"Repository '{name}' must use owner/repo format.")
        owner, repo = name.split("/", maxsplit=1)
        return owner.strip(), repo.strip()

    def _to_repository_snapshot(
        self, payload: dict[str, Any]
    ) -> RepositorySnapshot | None:
        stars = int(payload.get("stargazers_count", 0))
        size_kb = int(payload.get("size", 0))
        size_mb = round(size_kb / 1024.0, 2)
        is_fork = bool(payload.get("fork", False))

        if stars < self.config.min_stars:
            self._reject("repo_stars")
            return None
        if size_mb > self.config.max_repo_size_mb:
            self._reject("repo_size")
            return None
        if is_fork and not self.config.include_forks:
            self._reject("repo_fork")
            return None

        owner_block = payload.get("owner") or {}
        owner = owner_block.get("login")
        name = payload.get("name")
        if not owner or not name:
            self._reject("repo_shape")
            return None

        return RepositorySnapshot(
            owner=owner,
            name=name,
            stars=stars,
            size_mb=size_mb,
            html_url=payload.get("html_url", f"https://github.com/{owner}/{name}"),
            default_branch=payload.get("default_branch", "main"),
            description=payload.get("description"),
        )

    def _analyze_issue(
        self,
        repository: RepositorySnapshot,
        issue_payload: dict[str, Any],
    ) -> IssueCandidate | None:
        issue_number = int(issue_payload.get("number", 0))
        issue_title = (issue_payload.get("title") or "").strip()
        issue_body = issue_payload.get("body") or ""
        issue_body_length = len(issue_body.strip())
        issue_url = issue_payload.get("html_url", "")
        issue_state = issue_payload.get("state")

        if issue_state != "closed":
            self._reject("issue_not_closed")
            return None
        if not issue_title:
            self._reject("issue_no_title")
            return None
        if issue_body_length < self.config.min_issue_body_length:
            self._reject("issue_body_short")
            return None
        if contains_links_or_images(f"{issue_title}\n{issue_body}"):
            self._reject("issue_links_or_images")
            return None

        try:
            timeline = self.client.get_issue_timeline(
                repository.owner,
                repository.name,
                issue_number,
                pagination=PaginationConfig(max_pages=self.config.max_timeline_pages),
            )
        except GitHubClientError:
            self._reject("timeline_fetch")
            return None

        linked_pull_numbers = self._extract_linked_pull_numbers(timeline)
        search_pull_numbers = self._search_pull_numbers_referencing_issue(
            repository.owner, repository.name, issue_number
        )
        pull_numbers = dedupe_preserve_order(linked_pull_numbers + search_pull_numbers)
        if not pull_numbers:
            self._reject("linked_pull_count")
            return None

        valid_pull_payloads: list[dict[str, Any]] = []
        for pull_number in pull_numbers:
            try:
                pull_payload = self.client.get_pull_request(
                    repository.owner, repository.name, pull_number
                )
            except GitHubClientError:
                self._reject("pull_fetch")
                continue

            if pull_payload.get("merged_at") is None:
                continue

            closing_refs = extract_closing_issue_numbers(
                pull_payload.get("body") or "",
                owner=repository.owner,
                repo=repository.name,
            )
            if closing_refs == [issue_number]:
                valid_pull_payloads.append(pull_payload)

        if len(valid_pull_payloads) != 1:
            self._reject("pull_match_count")
            return None

        pull_payload = valid_pull_payloads[0]
        pull_number = int(pull_payload["number"])

        try:
            pull_files = self.client.get_pull_files(
                repository.owner,
                repository.name,
                pull_number,
                pagination=PaginationConfig(max_pages=self.config.max_pull_file_pages),
            )
        except GitHubClientError:
            self._reject("pull_files_fetch")
            return None

        python_non_test_files: list[FileChangeSummary] = []
        for file_payload in pull_files:
            path = file_payload.get("filename")
            if not isinstance(path, str):
                continue
            if not is_python_non_test_non_doc_file(path):
                continue
            python_non_test_files.append(
                FileChangeSummary(
                    path=path,
                    additions=int(file_payload.get("additions", 0)),
                    deletions=int(file_payload.get("deletions", 0)),
                    changes=int(file_payload.get("changes", 0)),
                )
            )

        if len(python_non_test_files) < self.config.min_python_files_changed:
            self._reject("python_non_test_file_count")
            return None

        max_python_file = max(python_non_test_files, key=lambda file: file.changes)
        if max_python_file.changes < self.config.min_single_python_file_changes:
            self._reject("python_single_file_changes")
            return None

        total_python_changes = sum(file.changes for file in python_non_test_files)
        complexity = score_complexity(
            python_non_test_files_changed=len(python_non_test_files),
            total_python_changes=total_python_changes,
            pr_commits=int(pull_payload.get("commits", 0)),
            max_single_python_file_changes=max_python_file.changes,
            issue_body_length=issue_body_length,
        )
        if complexity.score < self.config.min_complexity_score:
            self._reject("complexity")
            return None

        pull_snapshot = PullRequestSnapshot(
            number=int(pull_payload["number"]),
            html_url=pull_payload.get("html_url", ""),
            title=pull_payload.get("title", ""),
            base_sha=((pull_payload.get("base") or {}).get("sha") or ""),
            additions=int(pull_payload.get("additions", 0)),
            deletions=int(pull_payload.get("deletions", 0)),
            commits=int(pull_payload.get("commits", 0)),
            changed_files=int(pull_payload.get("changed_files", 0)),
            merged_at=pull_payload.get("merged_at"),
        )
        notes = [
            f"{len(python_non_test_files)} Python non-test files changed",
            f"largest Python file change: {max_python_file.path} ({max_python_file.changes} lines)",
            f"PR commits: {pull_snapshot.commits}",
            f"PR base SHA: {pull_snapshot.base_sha}",
        ]
        return IssueCandidate(
            repository=repository,
            issue_number=issue_number,
            issue_title=issue_title,
            issue_url=issue_url,
            issue_body_length=issue_body_length,
            pull_request=pull_snapshot,
            python_non_test_files=python_non_test_files,
            max_python_file_change=max_python_file,
            total_python_changes=total_python_changes,
            complexity=complexity,
            notes=notes,
        )

    def _extract_linked_pull_numbers(self, timeline: list[dict[str, Any]]) -> list[int]:
        pull_numbers: list[int] = []
        for event in timeline:
            source = event.get("source")
            if isinstance(source, dict):
                source_issue = source.get("issue")
                if isinstance(source_issue, dict) and source_issue.get("pull_request"):
                    number = source_issue.get("number")
                    if isinstance(number, int):
                        pull_numbers.append(number)
                    elif isinstance(number, str) and number.isdigit():
                        pull_numbers.append(int(number))

            subject = event.get("subject")
            if isinstance(subject, dict):
                subject_type = (subject.get("type") or "").lower()
                if subject_type == "pullrequest":
                    number = subject.get("number")
                    if isinstance(number, int):
                        pull_numbers.append(number)
                    elif isinstance(number, str) and number.isdigit():
                        pull_numbers.append(int(number))
                    else:
                        url = subject.get("url") or ""
                        maybe_number = self._pull_number_from_url(url)
                        if maybe_number is not None:
                            pull_numbers.append(maybe_number)

        return dedupe_preserve_order(pull_numbers)

    def _search_pull_numbers_referencing_issue(
        self, owner: str, repo: str, issue_number: int
    ) -> list[int]:
        try:
            results = self.client.search_merged_pull_requests_referencing_issue(
                owner, repo, issue_number
            )
        except GitHubClientError:
            self._reject("pull_search")
            return []

        numbers: list[int] = []
        for item in results:
            number = item.get("number")
            if isinstance(number, int):
                numbers.append(number)
            elif isinstance(number, str) and number.isdigit():
                numbers.append(int(number))

        return dedupe_preserve_order(numbers)

    @staticmethod
    def _pull_number_from_url(url: str) -> int | None:
        match = PULL_URL_NUMBER_PATTERN.search(url)
        if not match:
            return None
        return int(match.group(1))

    def _reject(self, key: str) -> None:
        self.rejection_counts[key] += 1
