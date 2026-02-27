from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from .types import PullFile, RepoRef


class GitHubApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubClient:
    token: str
    user_agent: str = "repoStar-issue-finder"
    base_url: str = "https://api.github.com"
    timeout_s: float = 30.0

    @classmethod
    def from_env(cls, *, token: str | None = None) -> "GitHubClient":
        tok = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not tok:
            raise GitHubApiError(
                "Missing GitHub token. Provide --github-token or set GITHUB_TOKEN (recommended) or GH_TOKEN."
            )
        return cls(token=tok)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": self.user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        url = f"{self.base_url}{path}"
        with httpx.Client(timeout=self.timeout_s) as client:
            resp = client.request(method, url, headers=self._headers(), params=params)
        if resp.status_code in (401, 403) and resp.headers.get("x-ratelimit-remaining") == "0":
            reset = resp.headers.get("x-ratelimit-reset")
            raise GitHubApiError(f"GitHub rate limit exceeded. Reset at epoch={reset}.")
        if resp.status_code >= 400:
            raise GitHubApiError(f"GitHub API error {resp.status_code}: {resp.text[:500]}")
        return resp

    def _paginate(self, path: str, *, params: dict[str, Any] | None = None) -> Iterable[dict[str, Any]]:
        page = 1
        while True:
            p = dict(params or {})
            p.setdefault("per_page", 100)
            p["page"] = page
            resp = self._request("GET", path, params=p)
            data = resp.json()
            if not isinstance(data, list):
                raise GitHubApiError(f"Expected list for paginated endpoint {path}, got {type(data)}")
            if not data:
                return
            for item in data:
                yield item
            page += 1
            # Gentle throttle to reduce secondary rate-limit risk.
            time.sleep(0.2)

    def search_repositories(self, *, query: str, max_repos: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while len(out) < max_repos:
            resp = self._request(
                "GET",
                "/search/repositories",
                params={"q": query, "sort": "stars", "order": "desc", "per_page": 100, "page": page},
            )
            payload = resp.json()
            items = payload.get("items") or []
            if not items:
                break
            out.extend(items)
            page += 1
            time.sleep(0.2)
        return out[:max_repos]

    def search_closed_issues(self, *, repo: RepoRef, max_issues: int) -> list[dict[str, Any]]:
        # GitHub search doesn't include full body reliably; we hydrate later.
        query = f"repo:{repo.full_name} is:issue is:closed"
        out: list[dict[str, Any]] = []
        page = 1
        while len(out) < max_issues:
            resp = self._request(
                "GET",
                "/search/issues",
                params={"q": query, "sort": "updated", "order": "desc", "per_page": 100, "page": page},
            )
            payload = resp.json()
            items = payload.get("items") or []
            if not items:
                break
            out.extend(items)
            page += 1
            time.sleep(0.2)
        return out[:max_issues]

    def get_issue(self, *, repo: RepoRef, number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{repo.owner}/{repo.name}/issues/{number}").json()

    def get_pull(self, *, repo: RepoRef, number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{repo.owner}/{repo.name}/pulls/{number}").json()

    def list_pull_files(self, *, repo: RepoRef, number: int, max_files: int = 500) -> list[PullFile]:
        files: list[PullFile] = []
        for item in self._paginate(f"/repos/{repo.owner}/{repo.name}/pulls/{number}/files"):
            files.append(
                PullFile(
                    filename=item.get("filename", ""),
                    status=item.get("status", ""),
                    additions=int(item.get("additions") or 0),
                    deletions=int(item.get("deletions") or 0),
                    changes=int(item.get("changes") or 0),
                )
            )
            if len(files) >= max_files:
                break
        return files

    def get_repo(self, *, repo: RepoRef) -> dict[str, Any]:
        return self._request("GET", f"/repos/{repo.owner}/{repo.name}").json()

    def graphql(self, *, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/graphql"
        with httpx.Client(timeout=self.timeout_s) as client:
            resp = client.post(url, headers=self._headers(), json={"query": query, "variables": variables})
        if resp.status_code >= 400:
            raise GitHubApiError(f"GitHub GraphQL error {resp.status_code}: {resp.text[:500]}")
        payload = resp.json()
        if "errors" in payload and payload["errors"]:
            raise GitHubApiError(f"GitHub GraphQL errors: {payload['errors']}")
        return payload.get("data") or {}

