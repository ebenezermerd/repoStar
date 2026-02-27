from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any


class GitHubClientError(RuntimeError):
    """Raised when gh api command fails."""


@dataclass(frozen=True)
class PaginationConfig:
    per_page: int = 100
    max_pages: int = 2


class GitHubClient:
    def __init__(self, gh_path: str = "gh", timeout_seconds: int = 60) -> None:
        self.gh_path = gh_path
        self.timeout_seconds = timeout_seconds

    def _run_api(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        accept_header: str | None = None,
    ) -> Any:
        command: list[str] = [self.gh_path, "api", endpoint, "--method", "GET"]
        if accept_header:
            command.extend(["-H", f"Accept: {accept_header}"])

        if params:
            for key, value in params.items():
                if isinstance(value, bool):
                    rendered = "true" if value else "false"
                else:
                    rendered = str(value)
                command.extend(["-f", f"{key}={rendered}"])

        process = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        if process.returncode != 0:
            raise GitHubClientError(
                f"gh api failed for '{endpoint}' "
                f"(exit={process.returncode}): {process.stderr.strip()}"
            )

        output = process.stdout.strip()
        if not output:
            return None

        try:
            return json.loads(output)
        except json.JSONDecodeError as error:
            raise GitHubClientError(
                f"Unable to parse gh api JSON for '{endpoint}': {error}"
            ) from error

    def search_repositories(
        self,
        query: str,
        *,
        min_stars: int,
        page_size: int = 25,
        max_pages: int = 2,
    ) -> list[dict[str, Any]]:
        repositories: list[dict[str, Any]] = []
        query_text = f"{query} stars:>={min_stars}"

        for page in range(1, max_pages + 1):
            payload = self._run_api(
                "search/repositories",
                params={
                    "q": query_text,
                    "sort": "stars",
                    "order": "desc",
                    "per_page": page_size,
                    "page": page,
                },
            )
            items = payload.get("items", []) if isinstance(payload, dict) else []
            repositories.extend(items)
            if len(items) < page_size:
                break

        return repositories

    def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        payload = self._run_api(f"repos/{owner}/{repo}")
        if not isinstance(payload, dict):
            raise GitHubClientError(f"Unexpected repository payload for {owner}/{repo}")
        return payload

    def list_closed_issues(
        self,
        owner: str,
        repo: str,
        *,
        max_issues: int,
        pagination: PaginationConfig | None = None,
    ) -> list[dict[str, Any]]:
        page_config = pagination or PaginationConfig()
        issues: list[dict[str, Any]] = []

        for page in range(1, page_config.max_pages + 1):
            payload = self._run_api(
                f"repos/{owner}/{repo}/issues",
                params={
                    "state": "closed",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": page_config.per_page,
                    "page": page,
                },
            )
            if not isinstance(payload, list):
                break

            page_items = [issue for issue in payload if "pull_request" not in issue]
            issues.extend(page_items)

            if len(issues) >= max_issues:
                return issues[:max_issues]

            if len(payload) < page_config.per_page:
                break

        return issues[:max_issues]

    def get_issue_timeline(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        pagination: PaginationConfig | None = None,
    ) -> list[dict[str, Any]]:
        page_config = pagination or PaginationConfig(max_pages=1)
        timeline: list[dict[str, Any]] = []

        for page in range(1, page_config.max_pages + 1):
            payload = self._run_api(
                f"repos/{owner}/{repo}/issues/{issue_number}/timeline",
                params={
                    "per_page": page_config.per_page,
                    "page": page,
                },
                accept_header="application/vnd.github+json",
            )
            if not isinstance(payload, list):
                break
            timeline.extend(payload)
            if len(payload) < page_config.per_page:
                break

        return timeline

    def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        payload = self._run_api(f"repos/{owner}/{repo}/pulls/{number}")
        if not isinstance(payload, dict):
            raise GitHubClientError(
                f"Unexpected pull request payload for {owner}/{repo}#{number}"
            )
        return payload

    def get_pull_files(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        pagination: PaginationConfig | None = None,
    ) -> list[dict[str, Any]]:
        page_config = pagination or PaginationConfig(max_pages=3)
        files: list[dict[str, Any]] = []

        for page in range(1, page_config.max_pages + 1):
            payload = self._run_api(
                f"repos/{owner}/{repo}/pulls/{number}/files",
                params={
                    "per_page": page_config.per_page,
                    "page": page,
                },
            )
            if not isinstance(payload, list):
                break
            files.extend(payload)
            if len(payload) < page_config.per_page:
                break

        return files

    def search_merged_pull_requests_referencing_issue(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        page_size: int = 20,
    ) -> list[dict[str, Any]]:
        query = f"repo:{owner}/{repo} is:pr is:merged #{issue_number}"
        payload = self._run_api(
            "search/issues",
            params={
                "q": query,
                "per_page": page_size,
                "page": 1,
            },
        )
        if not isinstance(payload, dict):
            return []
        items = payload.get("items")
        if not isinstance(items, list):
            return []
        return items
