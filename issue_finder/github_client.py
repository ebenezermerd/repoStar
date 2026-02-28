"""GitHub API client for fetching repos, issues, and PR data."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterator

from github import Github
from github.Repository import Repository


@dataclass
class RepoInfo:
    """Repository metadata from GitHub."""

    full_name: str
    stars: int
    size_kb: int
    language: str
    default_branch: str
    html_url: str
    description: str | None
    pushed_at: str | None


@dataclass
class IssueInfo:
    """Issue metadata from GitHub."""

    number: int
    title: str
    body: str | None
    state: str
    html_url: str
    created_at: str
    closed_at: str | None
    user_login: str
    comments_count: int
    labels: list[str]


@dataclass
class PRFileChange:
    """File change in a pull request."""

    filename: str
    additions: int
    deletions: int
    changes: int
    patch: str | None


@dataclass
class PRAnalysis:
    """Pull request with its file changes and closure info."""

    number: int
    html_url: str
    state: str
    merged: bool
    body: str | None
    files: list[PRFileChange]
    closes_issues: list[int]  # Issue numbers closed by "closes #N" etc.
    base_sha: str | None = None


class GitHubClient:
    """Client for GitHub API operations."""

    def __init__(self, token: str | None = None):
        self.token = token or os.environ.get("GITHUB_TOKEN")
        self.gh = Github(self.token) if self.token else Github()

    def search_python_repos(
        self,
        min_stars: int = 200,
        exclude_words: list[str] | None = None,
        max_results: int = 100,
    ) -> Iterator[RepoInfo]:
        """Search for Python repositories matching criteria."""
        exclude = exclude_words or ["collection", "list", "guide", "projects", "exercises"]
        exclude_query = " ".join(f"NOT {w}" for w in exclude)
        query = f"language:Python stars:>={min_stars} {exclude_query}"
        repos = self.gh.search_repositories(query=query, sort="stars", order="desc")
        count = 0
        for repo in repos:
            if count >= max_results:
                break
            try:
                yield RepoInfo(
                    full_name=repo.full_name,
                    stars=repo.stargazers_count,
                    size_kb=repo.size,  # API returns KB
                    language=repo.language or "",
                    default_branch=repo.default_branch,
                    html_url=repo.html_url,
                    description=repo.description,
                    pushed_at=repo.pushed_at.isoformat() if repo.pushed_at else None,
                )
                count += 1
            except Exception:
                continue

    def get_repo(self, full_name: str) -> Repository.Repository:
        """Get a repository by full name."""
        return self.gh.get_repo(full_name)

    def get_repo_info(self, full_name: str) -> RepoInfo | None:
        """Get repository info or None if not found."""
        try:
            repo = self.gh.get_repo(full_name)
            return RepoInfo(
                full_name=repo.full_name,
                stars=repo.stargazers_count,
                size_kb=repo.size,
                language=repo.language or "",
                default_branch=repo.default_branch,
                html_url=repo.html_url,
                description=repo.description,
                pushed_at=repo.pushed_at.isoformat() if repo.pushed_at else None,
            )
        except Exception:
            return None

    def get_closed_issues(
        self, full_name: str, state: str = "closed", max_issues: int = 200
    ) -> Iterator[IssueInfo]:
        """Get closed issues from a repository."""
        try:
            repo = self.gh.get_repo(full_name)
            issues = repo.get_issues(state=state, sort="updated", direction="desc")
            count = 0
            for issue in issues:
                if count >= max_issues:
                    break
                if issue.pull_request:
                    continue  # Skip PRs (issues endpoint returns both)
                try:
                    yield IssueInfo(
                        number=issue.number,
                        title=issue.title,
                        body=issue.body or "",
                        state=issue.state,
                        html_url=issue.html_url,
                        created_at=issue.created_at.isoformat(),
                        closed_at=issue.closed_at.isoformat() if issue.closed_at else None,
                        user_login=issue.user.login if issue.user else "",
                        comments_count=issue.comments,
                        labels=[lb.name for lb in issue.labels],
                    )
                    count += 1
                except Exception:
                    continue
        except Exception:
            return

    def get_prs_linked_to_issue(
        self, full_name: str, issue_number: int
    ) -> list:
        """Get PRs that reference/close an issue.

        Tries the scraper (Timeline API) first — one call instead of hundreds.
        Falls back to the legacy full-scan approach.
        """
        # Fast path: scraper uses Timeline API + HTML fallback
        try:
            from .scraper import GitHubScraper
            scraper = GitHubScraper(self.token)
            pr_nums = scraper.get_linked_prs(full_name, issue_number)
            if pr_nums:
                repo = self.gh.get_repo(full_name)
                prs = []
                for num in pr_nums:
                    try:
                        prs.append(repo.get_pull(num))
                    except Exception:
                        continue
                return prs
        except Exception:
            pass

        # Legacy fallback: iterate all closed PRs
        try:
            repo = self.gh.get_repo(full_name)
            prs = []
            for pr in repo.get_pulls(state="closed", sort="updated", direction="desc"):
                body = pr.body or ""
                if f"#{issue_number}" in body or f"# {issue_number}" in body:
                    prs.append(pr)
                if len(prs) >= 5:
                    break
            return prs
        except Exception:
            return []

    def get_pr_files(self, full_name: str, pr_number: int) -> list[PRFileChange]:
        """Get file changes for a pull request."""
        try:
            repo = self.gh.get_repo(full_name)
            pr = repo.get_pull(pr_number)
            files = []
            for f in pr.get_files():
                files.append(
                    PRFileChange(
                        filename=f.filename,
                        additions=f.additions,
                        deletions=f.deletions,
                        changes=f.changes,
                        patch=f.patch,
                    )
                )
            return files
        except Exception:
            return []

    def get_pr_body(self, full_name: str, pr_number: int) -> str | None:
        """Get PR body to check closure keywords."""
        try:
            repo = self.gh.get_repo(full_name)
            pr = repo.get_pull(pr_number)
            return pr.body
        except Exception:
            return None

    def get_pr_base_sha(self, full_name: str, pr_number: int) -> str | None:
        """Get the base commit SHA for a PR (checkout point before fix)."""
        try:
            repo = self.gh.get_repo(full_name)
            pr = repo.get_pull(pr_number)
            if hasattr(pr, "raw_data") and pr.raw_data:
                base = pr.raw_data.get("base", {})
                return base.get("sha")
            return getattr(getattr(pr, "base", None), "sha", None)
        except Exception:
            return None

    @staticmethod
    def parse_closes_keywords(body: str | None, issue_number: int) -> list[int]:
        """Parse 'closes #N', 'fixes #N', 'resolves #N' from PR body."""
        import re
        if not body:
            return []
        closed = []
        pattern = re.compile(
            r'(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s+#(\d+)',
            re.IGNORECASE
        )
        for m in pattern.finditer(body):
            closed.append(int(m.group(1)))
        return closed
