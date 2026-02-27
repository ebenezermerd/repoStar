"""GitHub API client with rate limiting and pagination support."""

import os
import time
import logging
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests

logger = logging.getLogger(__name__)

DEFAULT_PER_PAGE = 100
RATE_LIMIT_BUFFER = 10
BACKOFF_MULTIPLIER = 1.5


class GitHubAPIError(Exception):
    pass


class RateLimitExceeded(GitHubAPIError):
    pass


class GitHubClient:
    """Handles all communication with the GitHub REST API."""

    BASE_URL = "https://api.github.com"

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.environ.get("GITHUB_TOKEN") or self._get_gh_cli_token()
        if not self.token:
            raise GitHubAPIError(
                "No GitHub token found. Set GITHUB_TOKEN env var or install gh CLI."
            )
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        self._requests_remaining = None
        self._reset_time = None

    @staticmethod
    def _get_gh_cli_token() -> Optional[str]:
        import subprocess
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None

    def _update_rate_limit(self, response: requests.Response):
        remaining = response.headers.get("X-RateLimit-Remaining")
        reset = response.headers.get("X-RateLimit-Reset")
        if remaining is not None:
            self._requests_remaining = int(remaining)
        if reset is not None:
            self._reset_time = int(reset)

    def _wait_for_rate_limit(self):
        if self._requests_remaining is not None and self._requests_remaining < RATE_LIMIT_BUFFER:
            if self._reset_time:
                wait_seconds = max(0, self._reset_time - int(time.time())) + 5
                logger.warning(
                    "Rate limit low (%d remaining). Waiting %d seconds.",
                    self._requests_remaining, wait_seconds
                )
                time.sleep(wait_seconds)

    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        self._wait_for_rate_limit()
        url = f"{self.BASE_URL}{endpoint}" if endpoint.startswith("/") else endpoint
        retries = 3
        backoff = 2.0

        for attempt in range(retries + 1):
            try:
                response = self.session.request(method, url, timeout=30, **kwargs)
                self._update_rate_limit(response)

                if response.status_code == 403 and "rate limit" in response.text.lower():
                    if attempt < retries:
                        wait = max(backoff, self._reset_time - time.time() + 5) if self._reset_time else backoff
                        logger.warning("Rate limited. Retrying in %.1f seconds.", wait)
                        time.sleep(wait)
                        backoff *= BACKOFF_MULTIPLIER
                        continue
                    raise RateLimitExceeded("GitHub API rate limit exceeded")

                if response.status_code == 404:
                    return response

                response.raise_for_status()
                return response

            except requests.exceptions.RequestException as e:
                if attempt < retries:
                    logger.warning("Request failed (%s). Retrying in %.1f seconds.", e, backoff)
                    time.sleep(backoff)
                    backoff *= BACKOFF_MULTIPLIER
                    continue
                raise GitHubAPIError(f"Request failed after {retries} retries: {e}")

        raise GitHubAPIError("Max retries exceeded")

    def get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        response = self._request("GET", endpoint, params=params)
        if response.status_code == 404:
            return None
        return response.json()

    def get_paginated(self, endpoint: str, params: Optional[dict] = None,
                      max_pages: int = 10) -> list:
        """Fetch all pages of a paginated endpoint."""
        params = params or {}
        params.setdefault("per_page", DEFAULT_PER_PAGE)
        all_items = []

        for page in range(1, max_pages + 1):
            params["page"] = page
            response = self._request("GET", endpoint, params=params)
            if response.status_code == 404:
                break
            items = response.json()
            if not items:
                break
            if isinstance(items, dict) and "items" in items:
                all_items.extend(items["items"])
                if not items.get("incomplete_results", True) or len(items["items"]) < params["per_page"]:
                    break
            else:
                all_items.extend(items)
                if len(items) < params["per_page"]:
                    break

        return all_items

    def search_repositories(self, query: str, sort: str = "stars",
                            order: str = "desc", max_results: int = 100) -> list:
        params = {"q": query, "sort": sort, "order": order, "per_page": min(max_results, 100)}
        max_pages = (max_results + 99) // 100
        return self.get_paginated("/search/repositories", params=params, max_pages=max_pages)

    def get_repo(self, owner: str, repo: str) -> Optional[dict]:
        return self.get(f"/repos/{owner}/{repo}")

    def get_repo_size_mb(self, owner: str, repo: str) -> Optional[float]:
        """Get repo size in MB (GitHub reports size in KB)."""
        data = self.get_repo(owner, repo)
        if data:
            return data.get("size", 0) / 1024.0
        return None

    def get_closed_issues(self, owner: str, repo: str, max_pages: int = 5) -> list:
        return self.get_paginated(
            f"/repos/{owner}/{repo}/issues",
            params={"state": "closed", "sort": "updated", "direction": "desc"},
            max_pages=max_pages,
        )

    def get_issue(self, owner: str, repo: str, issue_number: int) -> Optional[dict]:
        return self.get(f"/repos/{owner}/{repo}/issues/{issue_number}")

    def get_issue_timeline(self, owner: str, repo: str, issue_number: int) -> list:
        """Get timeline events to find linked PRs."""
        return self.get_paginated(
            f"/repos/{owner}/{repo}/issues/{issue_number}/timeline",
            max_pages=5,
        )

    def get_issue_events(self, owner: str, repo: str, issue_number: int) -> list:
        return self.get_paginated(
            f"/repos/{owner}/{repo}/issues/{issue_number}/events",
            max_pages=3,
        )

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> Optional[dict]:
        return self.get(f"/repos/{owner}/{repo}/pulls/{pr_number}")

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list:
        return self.get_paginated(
            f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
            max_pages=5,
        )

    def get_rate_limit(self) -> dict:
        return self.get("/rate_limit")

    def check_rate_limit(self) -> tuple[int, int]:
        """Returns (remaining, limit) for core API."""
        data = self.get_rate_limit()
        core = data.get("resources", {}).get("core", {})
        return core.get("remaining", 0), core.get("limit", 0)
