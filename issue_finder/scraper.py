"""Web scraper for GitHub — bypasses REST API pagination bottlenecks.

Three major improvements over pure-API approach:
  1. Issue listing  – scrapes HTML with ?q=is:issue+is:closed (no PR noise)
  2. Linked PRs     – uses Timeline API or scrapes issue page (1 call vs hundreds)
  3. Repo search    – hits GitHub's internal JSON search endpoint
"""

from __future__ import annotations

import re
import time
import logging
import requests
from bs4 import BeautifulSoup

from .github_client import RepoInfo, IssueInfo, PRFileChange, PRAnalysis

log = logging.getLogger(__name__)

GITHUB = "https://github.com"
API = "https://api.github.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

HEAVY_DEPS = {
    "pytorch", "torch", "tensorflow", "keras", "jax", "mxnet",
    "cuda", "cupy", "triton", "onnxruntime", "paddlepaddle",
    "detectron2", "mmdet", "mmcv", "transformers", "diffusers",
    "deepspeed", "megatron", "fairseq", "espnet",
    "opencv-python-headless", "opencv-contrib-python",
    "dask", "ray", "spark", "pyspark", "hadoop",
}

BEST_REPO_SIGNALS = {
    "has_issues", "has_projects", "has_wiki",
}

# Regex helpers
_ISSUE_NUM = re.compile(r"/issues/(\d+)")
_PR_NUM = re.compile(r"/pull/(\d+)")
_CLOSES_KW = re.compile(
    r"(?:close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"
    r"\s+#(\d+)",
    re.IGNORECASE,
)


class GitHubScraper:
    """Scrapes GitHub web pages and lightweight API endpoints."""

    def __init__(self, token: str | None = None):
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        if token:
            self.session.headers["Authorization"] = f"token {token}"
        self._last_request = 0.0

    # ── Polite throttle ─────────────────────────────────────────

    def _get(self, url: str, *, accept: str = "text/html", timeout: int = 20) -> requests.Response:
        """GET with rate-limit courtesy pause and retries."""
        elapsed = time.monotonic() - self._last_request
        if elapsed < 0.5:
            time.sleep(0.5 - elapsed)

        headers = {"Accept": accept}
        for attempt in range(3):
            try:
                resp = self.session.get(url, headers=headers, timeout=timeout)
                self._last_request = time.monotonic()
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 5))
                    log.warning("Rate limited, waiting %ds", wait)
                    time.sleep(wait)
                    continue
                return resp
            except requests.RequestException as exc:
                if attempt == 2:
                    raise
                log.warning("Request failed (%s), retrying…", exc)
                time.sleep(2 ** attempt)

        raise RuntimeError(f"Failed after retries: {url}")

    # ── 1. Repo Search (JSON endpoint) ──────────────────────────

    def search_repos(
        self,
        query: str,
        language: str = "Python",
        min_stars: int = 200,
        max_results: int = 50,
    ) -> list[RepoInfo]:
        """Search GitHub repos via the internal JSON search endpoint."""
        results: list[RepoInfo] = []
        page = 1

        while len(results) < max_results:
            q = f"{query} language:{language} stars:>={min_stars}"
            url = f"{GITHUB}/search?q={requests.utils.quote(q)}&type=repositories&p={page}"
            resp = self._get(url, accept="application/json")

            if resp.status_code != 200:
                log.warning("Search returned %d", resp.status_code)
                break

            try:
                data = resp.json()
            except ValueError:
                break

            items = data.get("payload", {}).get("results", [])
            if not items:
                break

            for item in items:
                if len(results) >= max_results:
                    break
                repo_data = item.get("repo", {})
                repo_name = repo_data.get("repository", {}).get("nwo", "")
                if not repo_name:
                    hl = item.get("hl_name", "")
                    repo_name = re.sub(r"<[^>]+>", "", hl)

                if not repo_name:
                    continue

                results.append(RepoInfo(
                    full_name=repo_name,
                    stars=item.get("followers", 0),
                    size_kb=0,
                    language=item.get("language", {}).get("name", "") if isinstance(item.get("language"), dict) else str(item.get("language", "")),
                    default_branch="",
                    html_url=f"{GITHUB}/{repo_name}",
                    description=re.sub(r"<[^>]+>", "", item.get("hl_trunc_description", "") or ""),
                    pushed_at=None,
                ))

            page += 1
            if len(items) < 10:
                break

        return results

    # ── 2. Issue Listing (HTML scrape) ──────────────────────────

    def list_closed_issues(
        self,
        repo: str,
        max_pages: int = 5,
        max_issues: int = 100,
    ) -> list[IssueInfo]:
        """Scrape the issue listing page — returns real issues, not PRs.

        Uses the ?q=is:issue+is:closed filter which the REST API cannot do.
        """
        issues: list[IssueInfo] = []
        seen: set[int] = set()

        for page in range(1, max_pages + 1):
            if len(issues) >= max_issues:
                break

            url = (
                f"{GITHUB}/{repo}/issues"
                f"?q=is%3Aissue+is%3Aclosed&page={page}"
            )
            resp = self._get(url)
            if resp.status_code != 200:
                break

            page_issues = self._parse_issue_list(resp.text, repo)
            if not page_issues:
                break

            for iss in page_issues:
                if iss.number not in seen and len(issues) < max_issues:
                    seen.add(iss.number)
                    issues.append(iss)

        return issues

    def _parse_issue_list(self, html: str, repo: str) -> list[IssueInfo]:
        """Extract issues from listing page HTML."""
        issues: list[IssueInfo] = []
        soup = BeautifulSoup(html, "lxml")

        # Modern GitHub uses IssueRow containers
        rows = soup.find_all("div", class_=re.compile(r"IssueRow"))
        if rows:
            return self._parse_rows(rows, repo)

        # Fallback: find issue links directly
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            m = re.match(rf"/{re.escape(repo)}/issues/(\d+)$", href)
            if not m:
                continue
            title = a_tag.get_text(strip=True)
            if not title or len(title) < 3:
                continue

            num = int(m.group(1))
            if num in {i.number for i in issues}:
                continue

            labels = self._extract_labels_near(a_tag)

            issues.append(IssueInfo(
                number=num,
                title=title,
                body=None,
                state="closed",
                html_url=f"{GITHUB}/{repo}/issues/{num}",
                created_at="",
                closed_at=None,
                user_login="",
                comments_count=0,
                labels=labels,
            ))

        return issues

    def _parse_rows(self, rows, repo: str) -> list[IssueInfo]:
        """Parse modern GitHub IssueRow containers."""
        issues: list[IssueInfo] = []
        seen: set[int] = set()

        for row in rows:
            issue_link = row.find("a", href=re.compile(rf"/{re.escape(repo)}/issues/\d+$"))
            if not issue_link:
                continue
            m = re.search(r"/issues/(\d+)", issue_link["href"])
            if not m:
                continue
            num = int(m.group(1))
            if num in seen:
                continue
            seen.add(num)

            title = issue_link.get_text(strip=True)
            if not title or len(title) < 3:
                continue

            labels = self._extract_labels_near(row)

            issues.append(IssueInfo(
                number=num,
                title=title,
                body=None,
                state="closed",
                html_url=f"{GITHUB}/{repo}/issues/{num}",
                created_at="",
                closed_at=None,
                user_login="",
                comments_count=0,
                labels=labels,
            ))

        return issues

    @staticmethod
    def _extract_labels_near(element) -> list[str]:
        """Extract label names from an element or its ancestors."""
        labels: list[str] = []

        search = element
        for _ in range(5):
            if search is None:
                break
            # Label links contain label%3A in href
            for a_lbl in search.find_all("a", href=re.compile(r"label%3A")):
                # The actual name is inside a TokenTextContainer or similar span
                name_span = a_lbl.find("span", class_=re.compile(r"TokenText"))
                if name_span:
                    name = name_span.get_text(strip=True)
                else:
                    # Extract from URL: label%3A<name>
                    href = a_lbl.get("href", "")
                    lm = re.search(r"label%3A([^&+\s]+)", href)
                    name = lm.group(1) if lm else a_lbl.get_text(strip=True)

                if name and name not in labels and len(name) < 40:
                    labels.append(name)

            if labels:
                break
            search = search.parent

        return labels

    # ── 3. Issue Detail (HTML + body scrape) ────────────────────

    def get_issue_detail(self, repo: str, number: int) -> IssueInfo | None:
        """Scrape a single issue page for details and body text."""
        url = f"{GITHUB}/{repo}/issues/{number}"
        resp = self._get(url)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "lxml")

        title_el = soup.select_one(".js-issue-title, .gh-header-title")
        title = title_el.get_text(strip=True) if title_el else f"Issue #{number}"

        body_el = soup.select_one(".comment-body, .js-comment-body")
        body = body_el.get_text(strip=True) if body_el else ""

        state = "closed"
        state_el = soup.select_one('[title="Status: Closed"], .State--merged, .State--closed')
        if state_el:
            state = "closed"
        elif soup.select_one('[title="Status: Open"], .State--open'):
            state = "open"

        labels: list[str] = []
        for lbl in soup.select(".IssueLabel, .label"):
            labels.append(lbl.get_text(strip=True))

        return IssueInfo(
            number=number,
            title=title,
            body=body,
            state=state,
            html_url=url,
            created_at="",
            closed_at=None,
            user_login="",
            comments_count=0,
            labels=labels,
        )

    # ── 4. Linked PRs (Timeline API — the key improvement) ─────

    def get_linked_prs(self, repo: str, issue_number: int) -> list[int]:
        """Find PR numbers linked to an issue via the Timeline API.

        One API call replaces iterating hundreds of closed PRs.
        Falls back to scraping the issue page.
        """
        pr_numbers = self._linked_prs_from_timeline(repo, issue_number)
        if pr_numbers:
            return pr_numbers
        return self._linked_prs_from_html(repo, issue_number)

    def _linked_prs_from_timeline(self, repo: str, issue_number: int) -> list[int]:
        """Use /issues/{n}/timeline to find cross-referenced and closing PRs."""
        url = f"{API}/repos/{repo}/issues/{issue_number}/timeline"
        try:
            resp = self._get(url, accept="application/vnd.github.v3+json")
            if resp.status_code != 200:
                return []

            pr_nums: set[int] = set()
            for event in resp.json():
                etype = event.get("event", "")

                if etype == "cross-referenced":
                    source = event.get("source", {}).get("issue", {})
                    pr_data = source.get("pull_request")
                    if pr_data:
                        pr_url = pr_data.get("html_url", "")
                        m = _PR_NUM.search(pr_url)
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
        except Exception as exc:
            log.debug("Timeline API failed: %s", exc)
            return []

    def _linked_prs_from_html(self, repo: str, issue_number: int) -> list[int]:
        """Fallback: scrape the issue page for PR references."""
        url = f"{GITHUB}/{repo}/issues/{issue_number}"
        try:
            resp = self._get(url)
            if resp.status_code != 200:
                return []
            pr_nums: set[int] = set()
            for m in _PR_NUM.finditer(resp.text):
                pr_nums.add(int(m.group(1)))
            return sorted(pr_nums)
        except Exception:
            return []

    # ── 5. PR Detail (API + HTML fallback) ──────────────────────

    def get_pr_detail(self, repo: str, pr_number: int) -> PRAnalysis | None:
        """Fetch full PR analysis using the REST API."""
        base_url = f"{API}/repos/{repo}/pulls/{pr_number}"
        try:
            resp = self._get(base_url, accept="application/vnd.github.v3+json")
            if resp.status_code != 200:
                return None
            pr = resp.json()

            body = pr.get("body") or ""
            closes = [int(m) for m in _CLOSES_KW.findall(body)]
            base_sha = pr.get("base", {}).get("sha")
            merged = pr.get("merged", False)

            files = self._get_pr_files_api(repo, pr_number)

            return PRAnalysis(
                number=pr_number,
                html_url=pr.get("html_url", f"{GITHUB}/{repo}/pull/{pr_number}"),
                state=pr.get("state", "closed"),
                merged=merged,
                body=body,
                files=files,
                closes_issues=closes,
                base_sha=base_sha,
            )
        except Exception as exc:
            log.warning("PR detail failed: %s", exc)
            return None

    def _get_pr_files_api(self, repo: str, pr_number: int) -> list[PRFileChange]:
        """Fetch PR file changes via REST API."""
        url = f"{API}/repos/{repo}/pulls/{pr_number}/files"
        try:
            resp = self._get(url, accept="application/vnd.github.v3+json")
            if resp.status_code != 200:
                return []
            return [
                PRFileChange(
                    filename=f["filename"],
                    additions=f.get("additions", 0),
                    deletions=f.get("deletions", 0),
                    changes=f.get("changes", 0),
                    patch=f.get("patch"),
                )
                for f in resp.json()
            ]
        except Exception:
            return []

    # ── 6. PR Files via HTML scrape (fallback) ──────────────────

    def scrape_pr_files(self, repo: str, pr_number: int) -> list[PRFileChange]:
        """Scrape the PR files-changed page as a fallback."""
        url = f"{GITHUB}/{repo}/pull/{pr_number}/files"
        try:
            resp = self._get(url)
            if resp.status_code != 200:
                return []

            files: list[PRFileChange] = []
            soup = BeautifulSoup(resp.text, "lxml")

            for file_el in soup.select('[data-path], .file-header'):
                path = file_el.get("data-path", "")
                if not path:
                    a_tag = file_el.select_one("a[title]")
                    path = a_tag.get("title", "") if a_tag else ""
                if not path:
                    continue

                adds = dels = 0
                for span in file_el.parent.select(".diffstat") if file_el.parent else []:
                    text = span.get_text()
                    add_m = re.search(r"(\d+)\s*addition", text)
                    del_m = re.search(r"(\d+)\s*deletion", text)
                    if add_m:
                        adds = int(add_m.group(1))
                    if del_m:
                        dels = int(del_m.group(1))

                files.append(PRFileChange(
                    filename=path,
                    additions=adds,
                    deletions=dels,
                    changes=adds + dels,
                    patch=None,
                ))
            return files
        except Exception:
            return []

    # ── 7. Full issue analysis (scrape-powered pipeline) ────────

    def analyze_issue_fast(self, repo: str, issue_number: int) -> PRAnalysis | None:
        """Find the best linked PR for an issue using scraping.

        Replaces the old approach of iterating ALL closed PRs.
        Pipeline: Timeline API → linked PR numbers → PR detail.
        """
        pr_numbers = self.get_linked_prs(repo, issue_number)
        if not pr_numbers:
            return None

        for pr_num in pr_numbers:
            pr = self.get_pr_detail(repo, pr_num)
            if not pr:
                continue
            if issue_number in pr.closes_issues and len(pr.closes_issues) == 1:
                return pr

        for pr_num in pr_numbers:
            pr = self.get_pr_detail(repo, pr_num)
            if pr and issue_number in pr.closes_issues:
                return pr

        if pr_numbers:
            return self.get_pr_detail(repo, pr_numbers[0])

        return None

    # ── 8. Smart search presets ──────────────────────────────────

    def search_light_repos(
        self, query: str, min_stars: int = 200, max_results: int = 30,
    ) -> list[RepoInfo]:
        """Search for lightweight repos (small size, no heavy ML/CUDA deps)."""
        q = f"{query} language:Python stars:>={min_stars} size:<50000"
        url = f"{GITHUB}/search?q={requests.utils.quote(q)}&type=repositories&p=1"
        resp = self._get(url, accept="application/json")
        if resp.status_code != 200:
            return []
        try:
            items = resp.json().get("payload", {}).get("results", [])
        except ValueError:
            return []

        results: list[RepoInfo] = []
        for item in items:
            if len(results) >= max_results:
                break
            repo_data = item.get("repo", {})
            repo_name = repo_data.get("repository", {}).get("nwo", "")
            if not repo_name:
                hl = item.get("hl_name", "")
                repo_name = re.sub(r"<[^>]+>", "", hl)
            if not repo_name:
                continue
            desc = re.sub(r"<[^>]+>", "", item.get("hl_trunc_description", "") or "").lower()
            topics = [t.get("name", "") if isinstance(t, dict) else str(t) for t in item.get("topics", [])]
            combined = desc + " " + " ".join(topics)
            if any(dep in combined for dep in HEAVY_DEPS):
                continue
            results.append(RepoInfo(
                full_name=repo_name,
                stars=item.get("followers", 0),
                size_kb=0,
                language=item.get("language", {}).get("name", "") if isinstance(item.get("language"), dict) else str(item.get("language", "")),
                default_branch="",
                html_url=f"{GITHUB}/{repo_name}",
                description=re.sub(r"<[^>]+>", "", item.get("hl_trunc_description", "") or ""),
                pushed_at=None,
            ))
        return results

    def search_best_repos(
        self, query: str, min_stars: int = 500, max_results: int = 20,
    ) -> list[RepoInfo]:
        """Search for well-maintained repos: high stars, recent pushes, good issues."""
        q = f"{query} language:Python stars:>={min_stars} pushed:>2024-06-01 good-first-issues:>2"
        url = f"{GITHUB}/search?q={requests.utils.quote(q)}&type=repositories&s=stars&o=desc&p=1"
        resp = self._get(url, accept="application/json")
        if resp.status_code != 200:
            return []
        try:
            items = resp.json().get("payload", {}).get("results", [])
        except ValueError:
            return []

        results: list[RepoInfo] = []
        for item in items:
            if len(results) >= max_results:
                break
            repo_data = item.get("repo", {})
            repo_name = repo_data.get("repository", {}).get("nwo", "")
            if not repo_name:
                hl = item.get("hl_name", "")
                repo_name = re.sub(r"<[^>]+>", "", hl)
            if not repo_name:
                continue
            results.append(RepoInfo(
                full_name=repo_name,
                stars=item.get("followers", 0),
                size_kb=0,
                language=item.get("language", {}).get("name", "") if isinstance(item.get("language"), dict) else str(item.get("language", "")),
                default_branch="",
                html_url=f"{GITHUB}/{repo_name}",
                description=re.sub(r"<[^>]+>", "", item.get("hl_trunc_description", "") or ""),
                pushed_at=None,
            ))
        return results

    def search_by_label(
        self, repo: str, label: str, max_pages: int = 3, max_issues: int = 50,
    ) -> list[IssueInfo]:
        """List closed issues for a repo filtered by label."""
        issues: list[IssueInfo] = []
        seen: set[int] = set()
        label_encoded = requests.utils.quote(label)
        for page in range(1, max_pages + 1):
            if len(issues) >= max_issues:
                break
            url = (
                f"{GITHUB}/{repo}/issues"
                f"?q=is%3Aissue+is%3Aclosed+label%3A{label_encoded}&page={page}"
            )
            resp = self._get(url)
            if resp.status_code != 200:
                break
            page_issues = self._parse_issue_list(resp.text, repo)
            if not page_issues:
                break
            for iss in page_issues:
                if iss.number not in seen and len(issues) < max_issues:
                    seen.add(iss.number)
                    issues.append(iss)
        return issues

    # ── 9. Repo detail enrichment ───────────────────────────────

    def enrich_repo_info(self, info: RepoInfo) -> RepoInfo:
        """Fill in missing fields (size_kb, default_branch) via the API."""
        if info.size_kb and info.default_branch:
            return info
        url = f"{API}/repos/{info.full_name}"
        try:
            resp = self._get(url, accept="application/vnd.github.v3+json")
            if resp.status_code != 200:
                return info
            data = resp.json()
            return RepoInfo(
                full_name=info.full_name,
                stars=data.get("stargazers_count", info.stars),
                size_kb=data.get("size", info.size_kb),
                language=data.get("language", info.language) or info.language,
                default_branch=data.get("default_branch", info.default_branch) or info.default_branch,
                html_url=data.get("html_url", info.html_url),
                description=data.get("description", info.description),
                pushed_at=info.pushed_at,
            )
        except Exception:
            return info
