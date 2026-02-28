"""Async GitHub client — parallel repo/issue/PR analysis via aiohttp.

Replaces the sequential scraper and PyGithub client with fully concurrent
operations while reusing the same data structures and HTML parsers.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict
from urllib.parse import quote as urlquote

import aiohttp
from bs4 import BeautifulSoup

from .cache import CacheStore, TTL_ISSUES, TTL_REPO, TTL_SEARCH
from .github_client import IssueInfo, PRAnalysis, PRFileChange, RepoInfo
from .profiles import ScoringProfile, PR_WRITER_PROFILE
from .issue_analyzer import (
    IssueAnalysisResult,
    _body_has_links_or_images,
    _count_code_python_files,
    _has_substantial_changes,
)
from .scraper import GitHubScraper  # reuse HTML parsers

log = logging.getLogger(__name__)

GITHUB = "https://github.com"
API = "https://api.github.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

_PR_NUM = re.compile(r"/pull/(\d+)")
_CLOSES_KW = re.compile(
    r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)\s+#(\d+)",
    re.IGNORECASE,
)


class AsyncGitHubClient:
    """Fully async GitHub client with caching and concurrency control."""

    def __init__(
        self,
        token: str | None = None,
        cache: CacheStore | None = None,
        concurrency: int | None = None,
        scrape_concurrency: int | None = None,
    ):
        self.token = token
        self.cache = cache or CacheStore(enabled=False)
        # Auto-tune concurrency: with token we can be faster, without we must be gentle
        if token:
            api_c = concurrency or 10
            scrape_c = scrape_concurrency or 5
            self._throttle = 0.15
        else:
            api_c = concurrency or 2
            scrape_c = scrape_concurrency or 2
            self._throttle = 1.0  # 1 req/sec without token
        self._api_sem = asyncio.Semaphore(api_c)
        self._scrape_sem = asyncio.Semaphore(scrape_c)
        self._session: aiohttp.ClientSession | None = None
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        # Reuse sync scraper's HTML parsers
        self._scraper = GitHubScraper(token)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"User-Agent": UA}
            if self.token:
                headers["Authorization"] = f"token {self.token}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(
        self,
        url: str,
        *,
        accept: str = "text/html",
        is_api: bool = False,
        timeout: int = 20,
    ) -> tuple[int, str, dict]:
        """GET with rate limiting, retries, and semaphore control."""
        sem = self._api_sem if is_api else self._scrape_sem
        session = await self._ensure_session()

        async with sem:
            # Global throttle — serializes the actual HTTP send timing
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_request
                if elapsed < self._throttle:
                    await asyncio.sleep(self._throttle - elapsed)

            headers = {"Accept": accept}
            for attempt in range(3):
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                        self._last_request = time.monotonic()
                        if resp.status == 429:
                            wait = int(resp.headers.get("Retry-After", 5))
                            log.warning("Rate limited, waiting %ds", wait)
                            await asyncio.sleep(wait)
                            continue
                        body = await resp.text()
                        return resp.status, body, dict(resp.headers)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    if attempt == 2:
                        log.warning("Request failed after retries: %s", exc)
                        return 0, "", {}
                    await asyncio.sleep(2 ** attempt)

        return 0, "", {}

    async def _get_json(self, url: str, **kwargs) -> dict | list | None:
        status, body, _ = await self._get(url, accept="application/vnd.github.v3+json", is_api=True, **kwargs)
        if status != 200:
            return None
        try:
            import json
            return json.loads(body)
        except Exception:
            return None

    # ── Repo Operations ──────────────────────────────────────────

    async def search_repos(
        self,
        query: str,
        language: str = "Python",
        min_stars: int = 200,
        max_results: int = 50,
    ) -> list[RepoInfo]:
        cache_key = f"{query}:{language}:{min_stars}:{max_results}"
        cached = await self.cache.get("repos", cache_key)
        if cached:
            return [RepoInfo(**r) for r in cached]

        results: list[RepoInfo] = []
        page = 1

        while len(results) < max_results:
            q = f"{query} language:{language} stars:>={min_stars}"
            url = f"{GITHUB}/search?q={urlquote(q)}&type=repositories&p={page}"
            status, body, _ = await self._get(url, accept="application/json")

            if status != 200:
                break
            try:
                import json
                data = json.loads(body)
            except ValueError:
                break

            items = data.get("payload", {}).get("results", [])
            if not items:
                break

            for item in items:
                if len(results) >= max_results:
                    break
                repo_name = self._extract_repo_name(item)
                if not repo_name:
                    continue
                results.append(RepoInfo(
                    full_name=repo_name,
                    stars=item.get("followers", 0),
                    size_kb=0,
                    language=self._extract_language(item),
                    default_branch="",
                    html_url=f"{GITHUB}/{repo_name}",
                    description=re.sub(r"<[^>]+>", "", item.get("hl_trunc_description", "") or ""),
                    pushed_at=None,
                ))
            page += 1
            if len(items) < 10:
                break

        if results:
            await self.cache.set("repos", cache_key, [self._repo_dict(r) for r in results], TTL_SEARCH)
        return results

    async def get_repo_info(self, full_name: str) -> RepoInfo | None:
        cached = await self.cache.get("repo_info", full_name)
        if cached:
            return RepoInfo(**cached)

        data = await self._get_json(f"{API}/repos/{full_name}")
        if not data or not isinstance(data, dict):
            return None

        info = RepoInfo(
            full_name=data.get("full_name", full_name),
            stars=data.get("stargazers_count", 0),
            size_kb=data.get("size", 0),
            language=data.get("language") or "",
            default_branch=data.get("default_branch") or "main",
            html_url=data.get("html_url", f"{GITHUB}/{full_name}"),
            description=data.get("description"),
            pushed_at=data.get("pushed_at"),
        )
        await self.cache.set("repo_info", full_name, self._repo_dict(info), TTL_REPO)
        return info

    async def enrich_repo(self, repo: RepoInfo) -> RepoInfo:
        if repo.size_kb and repo.default_branch:
            return repo
        enriched = await self.get_repo_info(repo.full_name)
        return enriched or repo

    # ── Issue Operations ─────────────────────────────────────────

    async def list_closed_issues(
        self, repo: str, max_pages: int = 5, max_issues: int = 100,
    ) -> list[IssueInfo]:
        cache_key = f"{repo}:{max_issues}"
        cached = await self.cache.get("issues", cache_key)
        if cached:
            return [IssueInfo(**i) for i in cached]

        issues: list[IssueInfo] = []
        seen: set[int] = set()

        for page in range(1, max_pages + 1):
            if len(issues) >= max_issues:
                break
            url = f"{GITHUB}/{repo}/issues?q=is%3Aissue+is%3Aclosed&page={page}"
            status, body, _ = await self._get(url)
            if status != 200:
                break
            page_issues = self._scraper._parse_issue_list(body, repo)
            if not page_issues:
                break
            for iss in page_issues:
                if iss.number not in seen and len(issues) < max_issues:
                    seen.add(iss.number)
                    issues.append(iss)

        if issues:
            await self.cache.set("issues", cache_key, [self._issue_dict(i) for i in issues], TTL_ISSUES)
        return issues

    async def get_issue_detail(self, repo: str, number: int) -> IssueInfo | None:
        cache_key = f"{repo}#{number}"
        cached = await self.cache.get("issue_detail", cache_key)
        if cached:
            return IssueInfo(**cached)

        url = f"{GITHUB}/{repo}/issues/{number}"
        status, body, _ = await self._get(url)
        if status != 200:
            return None

        soup = BeautifulSoup(body, "lxml")
        title_el = soup.select_one(".js-issue-title, .gh-header-title")
        title = title_el.get_text(strip=True) if title_el else f"Issue #{number}"
        body_el = soup.select_one(".comment-body, .js-comment-body")
        body_text = body_el.get_text(strip=True) if body_el else ""

        state = "closed"
        if soup.select_one('[title="Status: Open"], .State--open'):
            state = "open"

        labels: list[str] = []
        for lbl in soup.select(".IssueLabel, .label"):
            labels.append(lbl.get_text(strip=True))

        info = IssueInfo(
            number=number, title=title, body=body_text, state=state,
            html_url=f"{GITHUB}/{repo}/issues/{number}",
            created_at="", closed_at=None, user_login="",
            comments_count=0, labels=labels,
        )
        await self.cache.set("issue_detail", cache_key, self._issue_dict(info), TTL_ISSUES)
        return info

    # ── PR Operations ────────────────────────────────────────────

    async def get_linked_prs(self, repo: str, issue_number: int) -> list[int]:
        cache_key = f"{repo}#{issue_number}:prs"
        cached = await self.cache.get("linked_prs", cache_key)
        if cached:
            return cached

        # Try Timeline API first
        pr_nums = await self._linked_prs_timeline(repo, issue_number)
        if not pr_nums:
            pr_nums = await self._linked_prs_html(repo, issue_number)

        if pr_nums:
            await self.cache.set("linked_prs", cache_key, pr_nums, TTL_ISSUES)
        return pr_nums

    async def _linked_prs_timeline(self, repo: str, issue_number: int) -> list[int]:
        url = f"{API}/repos/{repo}/issues/{issue_number}/timeline"
        data = await self._get_json(url)
        if not data or not isinstance(data, list):
            return []

        pr_nums: set[int] = set()
        for event in data:
            etype = event.get("event", "")
            if etype == "cross-referenced":
                source = event.get("source", {}).get("issue", {})
                pr_data = source.get("pull_request")
                if pr_data:
                    m = _PR_NUM.search(pr_data.get("html_url", ""))
                    if m:
                        pr_nums.add(int(m.group(1)))
            elif etype == "closed":
                closer = event.get("source", {}) or {}
                closer_pr = closer.get("issue", {}).get("pull_request")
                if closer_pr:
                    m = _PR_NUM.search(closer_pr.get("html_url", ""))
                    if m:
                        pr_nums.add(int(m.group(1)))
        return sorted(pr_nums)

    async def _linked_prs_html(self, repo: str, issue_number: int) -> list[int]:
        url = f"{GITHUB}/{repo}/issues/{issue_number}"
        status, body, _ = await self._get(url)
        if status != 200:
            return []
        pr_nums: set[int] = set()
        for m in _PR_NUM.finditer(body):
            pr_nums.add(int(m.group(1)))
        return sorted(pr_nums)

    async def get_pr_detail(self, repo: str, pr_number: int) -> PRAnalysis | None:
        cache_key = f"{repo}#{pr_number}"
        cached = await self.cache.get("pr_detail", cache_key)
        if cached:
            cached["files"] = [PRFileChange(**f) for f in cached.get("files", [])]
            return PRAnalysis(**cached)

        data = await self._get_json(f"{API}/repos/{repo}/pulls/{pr_number}")
        if not data or not isinstance(data, dict):
            return None

        body = data.get("body") or ""
        closes = [int(m) for m in _CLOSES_KW.findall(body)]
        base_sha = data.get("base", {}).get("sha")
        merged = data.get("merged", False)

        files = await self._get_pr_files(repo, pr_number)

        pr = PRAnalysis(
            number=pr_number,
            html_url=data.get("html_url", f"{GITHUB}/{repo}/pull/{pr_number}"),
            state=data.get("state", "closed"),
            merged=merged,
            body=body,
            files=files,
            closes_issues=closes,
            base_sha=base_sha,
        )
        # Cache with serializable files
        pr_cache = {
            "number": pr.number, "html_url": pr.html_url, "state": pr.state,
            "merged": pr.merged, "body": pr.body,
            "files": [{"filename": f.filename, "additions": f.additions, "deletions": f.deletions, "changes": f.changes, "patch": f.patch} for f in pr.files],
            "closes_issues": pr.closes_issues, "base_sha": pr.base_sha,
        }
        await self.cache.set("pr_detail", cache_key, pr_cache, TTL_ISSUES)
        return pr

    async def _get_pr_files(self, repo: str, pr_number: int) -> list[PRFileChange]:
        data = await self._get_json(f"{API}/repos/{repo}/pulls/{pr_number}/files")
        if not data or not isinstance(data, list):
            return []
        return [
            PRFileChange(
                filename=f["filename"],
                additions=f.get("additions", 0),
                deletions=f.get("deletions", 0),
                changes=f.get("changes", 0),
                patch=f.get("patch"),
            )
            for f in data
        ]

    # ── Batch Analysis ───────────────────────────────────────────

    async def analyze_issue(
        self,
        repo: str,
        issue: IssueInfo,
        profile: ScoringProfile | None = None,
    ) -> IssueAnalysisResult:
        """Analyze a single issue — fully async version of IssueAnalyzer.analyze_issue."""
        profile = profile or PR_WRITER_PROFILE
        reasons: list[str] = []
        details: dict = {}
        score = 0.0

        # Gate 1: must be closed
        if issue.state != "closed":
            return IssueAnalysisResult(issue=issue, pr_analysis=None, passes=False, reasons=["Issue is not closed"])

        # Fetch body if missing
        if issue.body is None:
            detail = await self.get_issue_detail(repo, issue.number)
            if detail:
                issue = detail

        # Gate 2: pure body check
        if profile.require_pure_body and _body_has_links_or_images(issue.body):
            reasons.append("Issue body contains links or images")
        else:
            score += profile.pure_body_score
            reasons.append("Body is pure text")

        # Gate 3: find linked PRs
        pr_nums = await self.get_linked_prs(repo, issue.number)
        if not pr_nums:
            reasons.append("No PR found that references this issue")
            return IssueAnalysisResult(issue=issue, pr_analysis=None, passes=False, reasons=reasons, details=details, score=score)

        # Gate 4: one-way closure
        best_pr: PRAnalysis | None = None
        for pr_num in pr_nums:
            pr = await self.get_pr_detail(repo, pr_num)
            if not pr:
                continue
            if issue.number not in pr.closes_issues:
                continue
            if profile.require_one_way_close and len(pr.closes_issues) > 1:
                reasons.append(f"PR #{pr_num} closes multiple issues: {pr.closes_issues}")
                continue
            best_pr = pr
            break

        if not best_pr:
            reasons.append("No PR with one-way close")
            return IssueAnalysisResult(issue=issue, pr_analysis=None, passes=False, reasons=reasons, details=details, score=score)

        # Score: code files
        code_files = _count_code_python_files(best_pr.files)
        details["code_python_files_changed"] = code_files
        if code_files < profile.min_code_files_changed:
            reasons.append(f"Only {code_files} Python code files changed (need >= {profile.min_code_files_changed})")
        else:
            score += profile.code_files_score
            reasons.append(f"{code_files} Python code files changed")

        # Score: substantial changes
        if not _has_substantial_changes(best_pr.files, profile.min_substantial_changes):
            reasons.append(f"No code file has >= {profile.min_substantial_changes} lines changed")
        else:
            score += profile.substantial_changes_score
            reasons.append("At least one code file has substantial changes")

        # Complexity hint
        total_adds = sum(f.additions for f in best_pr.files)
        total_dels = sum(f.deletions for f in best_pr.files)
        details["total_additions"] = total_adds
        details["total_deletions"] = total_dels
        total = total_adds + total_dels
        if total > 100:
            complexity = "High complexity"
        elif total > 50:
            complexity = "Medium-high complexity"
        elif total > 20:
            complexity = "Medium complexity"
        else:
            complexity = "May be too simple"

        # Title and body quality
        if len(issue.title) >= 10:
            score += profile.good_title_score
        else:
            reasons.append("Issue title may be too vague")

        if issue.body and len(issue.body) > 50:
            score += profile.good_description_score
            reasons.append("Issue has substantive description")
        elif not issue.body or len(issue.body) < 20:
            reasons.append("Issue description may be too brief")

        passes = (
            code_files >= profile.min_code_files_changed
            and _has_substantial_changes(best_pr.files, profile.min_substantial_changes)
        )

        return IssueAnalysisResult(
            issue=issue, pr_analysis=best_pr, passes=passes,
            reasons=reasons, details=details, score=score,
            complexity_hint=complexity,
        )

    async def scan_repo(
        self,
        repo: str,
        profile: ScoringProfile | None = None,
        max_issues: int = 100,
        pre_filter=None,
    ) -> list[IssueAnalysisResult]:
        """Scan a repo: list issues → pre-filter → analyze in parallel."""
        profile = profile or PR_WRITER_PROFILE
        issues = await self.list_closed_issues(repo, max_issues=max_issues)

        # Apply pre-filter
        if pre_filter:
            issues = [i for i in issues if pre_filter(i, profile)]

        # Analyze concurrently
        tasks = [self.analyze_issue(repo, issue, profile) for issue in issues]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        passing: list[IssueAnalysisResult] = []
        for r in results:
            if isinstance(r, Exception):
                log.debug("Analysis error: %s", r)
                continue
            if r.passes and r.score >= profile.min_score:
                passing.append(r)

        passing.sort(key=lambda x: x.score, reverse=True)
        return passing

    async def scan_repos_parallel(
        self,
        repos: list[RepoInfo],
        profile: ScoringProfile | None = None,
        max_issues_per_repo: int = 50,
        pre_filter=None,
        on_repo_done=None,
    ) -> list[IssueAnalysisResult]:
        """Scan multiple repos in parallel."""
        profile = profile or PR_WRITER_PROFILE
        all_results: list[IssueAnalysisResult] = []

        async def _scan_one(repo: RepoInfo):
            try:
                enriched = await self.enrich_repo(repo)
                # Quick repo validation
                if enriched.size_kb > profile.max_size_mb * 1024:
                    return []
                if enriched.stars < profile.min_stars:
                    return []
                if profile.required_language and enriched.language.lower() != profile.required_language.lower():
                    return []
                results = await self.scan_repo(
                    enriched.full_name, profile,
                    max_issues=max_issues_per_repo,
                    pre_filter=pre_filter,
                )
                if on_repo_done:
                    on_repo_done(enriched, results)
                return results
            except Exception as e:
                log.debug("Scan failed for %s: %s", repo.full_name, e)
                return []

        tasks = [_scan_one(r) for r in repos]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in batch_results:
            if isinstance(r, list):
                all_results.extend(r)

        all_results.sort(key=lambda x: x.score, reverse=True)
        return all_results

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _extract_repo_name(item: dict) -> str:
        repo_data = item.get("repo", {})
        name = repo_data.get("repository", {}).get("nwo", "")
        if not name:
            hl = item.get("hl_name", "")
            name = re.sub(r"<[^>]+>", "", hl)
        return name

    @staticmethod
    def _extract_language(item: dict) -> str:
        lang = item.get("language")
        if isinstance(lang, dict):
            return lang.get("name", "")
        return str(lang or "")

    @staticmethod
    def _repo_dict(r: RepoInfo) -> dict:
        return {
            "full_name": r.full_name, "stars": r.stars, "size_kb": r.size_kb,
            "language": r.language, "default_branch": r.default_branch,
            "html_url": r.html_url, "description": r.description,
            "pushed_at": r.pushed_at,
        }

    @staticmethod
    def _issue_dict(i: IssueInfo) -> dict:
        return {
            "number": i.number, "title": i.title, "body": i.body,
            "state": i.state, "html_url": i.html_url,
            "created_at": i.created_at, "closed_at": i.closed_at,
            "user_login": i.user_login, "comments_count": i.comments_count,
            "labels": i.labels,
        }
