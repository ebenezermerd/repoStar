"""Core analysis engine: orchestrates API calls and filtering to evaluate issues."""

import re
import logging
from typing import Optional

from .github_api import GitHubClient
from .filters import (
    IssueAnalysis,
    analyze_file_changes,
    check_issue_text_purity,
)

logger = logging.getLogger(__name__)

MIN_STARS = 200
MAX_REPO_SIZE_MB = 200.0
MIN_PYTHON_CODE_FILES = 4
MIN_SUBSTANTIAL_CHANGES = 20


class IssueAnalyzer:
    """Analyzes GitHub issues against PR Writer project criteria."""

    def __init__(self, client: GitHubClient, min_stars: int = MIN_STARS,
                 max_size_mb: float = MAX_REPO_SIZE_MB,
                 min_python_files: int = MIN_PYTHON_CODE_FILES,
                 min_substantial: int = MIN_SUBSTANTIAL_CHANGES):
        self.client = client
        self.min_stars = min_stars
        self.max_size_mb = max_size_mb
        self.min_python_files = min_python_files
        self.min_substantial = min_substantial

    def analyze_issue(self, owner: str, repo: str, issue_number: int,
                      repo_info: Optional[dict] = None,
                      issue_data: Optional[dict] = None,
                      early_reject: bool = True) -> IssueAnalysis:
        """Fully analyze an issue against all criteria.

        Args:
            issue_data: Pre-fetched issue data from list endpoint to avoid extra API call.
            early_reject: If True, skip expensive API calls when cheap checks fail.
        """
        analysis = IssueAnalysis(owner=owner, repo=repo, issue_number=issue_number)

        if repo_info is None:
            repo_info = self.client.get_repo(owner, repo)
        if not repo_info:
            analysis.rejection_reasons.append("Repository not found")
            return analysis

        analysis.repo_stars = repo_info.get("stargazers_count", 0)
        analysis.repo_size_mb = repo_info.get("size", 0) / 1024.0

        if analysis.repo_stars < self.min_stars:
            analysis.rejection_reasons.append(
                f"Stars ({analysis.repo_stars}) below minimum ({self.min_stars})"
            )
        if analysis.repo_size_mb > self.max_size_mb:
            analysis.rejection_reasons.append(
                f"Repo size ({analysis.repo_size_mb:.1f}MB) exceeds maximum ({self.max_size_mb}MB)"
            )

        if early_reject and analysis.rejection_reasons:
            return analysis

        if issue_data is None:
            issue_data = self.client.get_issue(owner, repo, issue_number)
        if not issue_data:
            analysis.rejection_reasons.append("Issue not found")
            return analysis

        if issue_data.get("pull_request"):
            analysis.rejection_reasons.append("This is a pull request, not an issue")
            return analysis

        analysis.issue_title = issue_data.get("title", "")
        analysis.issue_url = issue_data.get("html_url", "")
        analysis.issue_body = issue_data.get("body", "") or ""

        if issue_data.get("state") != "closed":
            analysis.rejection_reasons.append("Issue is not closed")

        has_images, has_links, link_count = check_issue_text_purity(analysis.issue_body)
        analysis.has_images = has_images
        analysis.has_links = has_links
        analysis.link_count = link_count
        analysis.is_pure_text = not has_images and not has_links

        if has_images:
            analysis.rejection_reasons.append("Issue description contains images")
        if has_links:
            analysis.rejection_reasons.append(
                f"Issue description contains links ({link_count} found)"
            )

        if not analysis.issue_body or len(analysis.issue_body.strip()) < 30:
            analysis.rejection_reasons.append("Issue description is too short or empty")

        if early_reject and analysis.rejection_reasons:
            return analysis

        linked_prs = self._find_linked_prs(owner, repo, issue_number, issue_data)
        analysis.linked_pr_count = len(linked_prs)

        if len(linked_prs) == 0:
            analysis.rejection_reasons.append("No linked merged PR found")
            return analysis

        if len(linked_prs) > 1:
            analysis.rejection_reasons.append(
                f"Multiple merged PRs linked ({len(linked_prs)}): "
                f"{', '.join(f'#{n}' for n in linked_prs)}. "
                f"One-way link required (1 issue -> 1 PR)."
            )

        analysis.pr_number = linked_prs[0]
        self._analyze_pr(analysis, owner, repo, linked_prs[0])
        self._compute_complexity(analysis)

        analysis.meets_criteria = len(analysis.rejection_reasons) == 0
        return analysis

    def _find_linked_prs(self, owner: str, repo: str, issue_number: int,
                         issue_data: dict) -> list[int]:
        """Find all merged PRs from the same repo linked to this issue.

        Returns a deduplicated list of PR numbers. Uses multiple strategies:
        1. Timeline cross-references filtered to same repo + closing commit match
        2. Same-repo PRs whose body contains closing keywords for this issue
        3. Same-repo merged PRs that are cross-referenced
        4. GitHub search API fallback for PRs mentioning the issue
        """
        repo_url_prefix = f"https://github.com/{owner}/{repo}/"
        timeline = self.client.get_issue_timeline(owner, repo, issue_number)

        same_repo_prs = set()
        closing_commit_id = None

        for event in timeline:
            if not isinstance(event, dict):
                continue

            event_type = event.get("event")

            if event_type == "cross-referenced":
                source = event.get("source", {})
                issue_info = source.get("issue", {})
                html_url = issue_info.get("html_url", "")
                if issue_info.get("pull_request") and html_url.startswith(repo_url_prefix):
                    try:
                        pr_num = int(html_url.rstrip("/").split("/")[-1])
                        if issue_info.get("state") == "closed":
                            pr_meta = issue_info.get("pull_request", {})
                            if pr_meta.get("merged_at"):
                                same_repo_prs.add(pr_num)
                    except (ValueError, IndexError, KeyError):
                        pass

            elif event_type == "closed":
                cid = event.get("commit_id")
                if cid:
                    closing_commit_id = cid

        merged_prs = []

        # Strategy 1: match closing commit SHA to a same-repo PR merge commit
        if closing_commit_id and same_repo_prs:
            for pr_num in sorted(same_repo_prs):
                pr_data = self.client.get_pull_request(owner, repo, pr_num)
                if (pr_data and pr_data.get("merged")
                        and pr_data.get("merge_commit_sha") == closing_commit_id):
                    merged_prs.append(pr_num)
                    break

        # Strategy 2: same-repo PRs with closing keywords in body
        if not merged_prs and same_repo_prs:
            for pr_num in sorted(same_repo_prs):
                pr_data = self.client.get_pull_request(owner, repo, pr_num)
                if pr_data and pr_data.get("merged"):
                    body = pr_data.get("body") or ""
                    close_pattern = rf'(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*#{issue_number}\b'
                    if re.search(close_pattern, body, re.IGNORECASE):
                        merged_prs.append(pr_num)

        # Strategy 3: any same-repo merged PR that was cross-referenced
        if not merged_prs and same_repo_prs:
            for pr_num in sorted(same_repo_prs):
                pr_data = self.client.get_pull_request(owner, repo, pr_num)
                if pr_data and pr_data.get("merged"):
                    merged_prs.append(pr_num)

        # Strategy 4: GitHub search API fallback
        if not merged_prs:
            search_results = self.client.get(
                "/search/issues",
                params={
                    "q": f"repo:{owner}/{repo} is:pr is:merged {issue_number} in:body",
                    "per_page": 5,
                }
            )
            if search_results and isinstance(search_results, dict) and search_results.get("items"):
                for item in search_results["items"]:
                    body = item.get("body") or ""
                    close_pattern = rf'(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*#{issue_number}\b'
                    if re.search(close_pattern, body, re.IGNORECASE):
                        merged_prs.append(item["number"])

        unique_prs = list(dict.fromkeys(merged_prs))
        if len(unique_prs) > 1:
            logger.info(
                "Issue #%d has %d linked merged PRs from same repo: %s",
                issue_number, len(unique_prs), unique_prs
            )
        return unique_prs

    def _analyze_pr(self, analysis: IssueAnalysis, owner: str, repo: str, pr_number: int):
        """Analyze the linked PR's file changes."""
        pr_data = self.client.get_pull_request(owner, repo, pr_number)
        if not pr_data:
            analysis.rejection_reasons.append(f"PR #{pr_number} not found")
            return

        analysis.pr_url = pr_data.get("html_url", "")
        analysis.pr_merged = pr_data.get("merged", False)
        analysis.base_sha = pr_data.get("base", {}).get("sha", "")

        if not analysis.pr_merged:
            analysis.rejection_reasons.append(f"PR #{pr_number} was not merged")
            return

        pr_files = self.client.get_pr_files(owner, repo, pr_number)
        analysis.file_changes = analyze_file_changes(pr_files)

        python_code_files = []
        for fc in analysis.file_changes:
            if fc.is_code_python:
                python_code_files.append(fc)
            if fc.is_test:
                analysis.total_test_files_changed += 1
            if fc.is_doc:
                analysis.total_doc_files_changed += 1
            analysis.total_additions += fc.additions
            analysis.total_deletions += fc.deletions

            if fc.total_changes > analysis.max_file_changes:
                analysis.max_file_changes = fc.total_changes
                analysis.max_change_file = fc.filename

        analysis.total_python_code_files = len(python_code_files)

        if analysis.total_python_code_files < self.min_python_files:
            analysis.rejection_reasons.append(
                f"Only {analysis.total_python_code_files} Python code files changed "
                f"(minimum {self.min_python_files} required, excluding test/doc files)"
            )

        has_substantial = any(
            fc.total_changes >= self.min_substantial for fc in python_code_files
        )
        if python_code_files and not has_substantial:
            analysis.rejection_reasons.append(
                f"No Python code file has >= {self.min_substantial} line changes"
            )

    def _compute_complexity(self, analysis: IssueAnalysis):
        """Compute a complexity score based on multiple factors."""
        score = 0.0

        score += min(analysis.total_python_code_files * 5, 30)

        total_code_changes = sum(
            fc.total_changes for fc in analysis.file_changes if fc.is_code_python
        )
        if total_code_changes > 200:
            score += 20
        elif total_code_changes > 100:
            score += 15
        elif total_code_changes > 50:
            score += 10
        elif total_code_changes > 20:
            score += 5

        if analysis.max_file_changes > 100:
            score += 10
        elif analysis.max_file_changes > 50:
            score += 7
        elif analysis.max_file_changes > 20:
            score += 4

        body_len = len(analysis.issue_body) if analysis.issue_body else 0
        if body_len > 500:
            score += 10
        elif body_len > 200:
            score += 7
        elif body_len > 100:
            score += 4

        unique_dirs = set()
        for fc in analysis.file_changes:
            if fc.is_code_python:
                parts = fc.filename.rsplit("/", 1)
                if len(parts) > 1:
                    unique_dirs.add(parts[0])
        score += min(len(unique_dirs) * 3, 15)

        if analysis.total_test_files_changed > 0:
            score += 5

        analysis.complexity_score = score

    def search_and_analyze(self, query: str = None, max_repos: int = 10,
                           max_issues_per_repo: int = 20,
                           min_complexity: float = 0.0) -> list[IssueAnalysis]:
        """Search for repos and analyze their closed issues."""
        if query is None:
            query = "language:Python stars:>=200 NOT collection NOT list NOT guide NOT projects NOT exercises"

        logger.info("Searching repositories with query: %s", query)
        repos = self.client.search_repositories(query, max_results=max_repos)
        logger.info("Found %d repositories", len(repos))

        results = []
        for repo_data in repos:
            owner = repo_data["owner"]["login"]
            repo_name = repo_data["name"]
            stars = repo_data.get("stargazers_count", 0)
            size_mb = repo_data.get("size", 0) / 1024.0

            if stars < self.min_stars:
                continue
            if size_mb > self.max_size_mb:
                logger.info("Skipping %s/%s: size %.1fMB exceeds limit", owner, repo_name, size_mb)
                continue

            logger.info("Analyzing %s/%s (stars=%d, size=%.1fMB)", owner, repo_name, stars, size_mb)
            issues = self.client.get_closed_issues(owner, repo_name, max_pages=2)

            issue_count = 0
            for issue_data in issues:
                if issue_data.get("pull_request"):
                    continue
                if issue_count >= max_issues_per_repo:
                    break
                issue_count += 1

                issue_number = issue_data["number"]
                logger.info("  Analyzing issue #%d: %s", issue_number, issue_data.get("title", "")[:60])

                analysis = self.analyze_issue(
                    owner, repo_name, issue_number,
                    repo_info=repo_data, issue_data=issue_data, early_reject=True,
                )

                if analysis.meets_criteria and analysis.complexity_score >= min_complexity:
                    results.append(analysis)
                    logger.info("  PASS Issue #%d meets criteria (score=%.1f)", issue_number, analysis.complexity_score)
                elif analysis.meets_criteria:
                    logger.info("  LOW Issue #%d meets criteria but low complexity (score=%.1f)", issue_number, analysis.complexity_score)
                else:
                    reasons = "; ".join(analysis.rejection_reasons[:3])
                    logger.debug("  FAIL Issue #%d rejected: %s", issue_number, reasons)

        results.sort(key=lambda a: a.complexity_score, reverse=True)
        return results

    def analyze_specific_repo(self, owner: str, repo: str,
                              max_issues: int = 50,
                              min_complexity: float = 0.0) -> list[IssueAnalysis]:
        """Analyze closed issues from a specific repository."""
        repo_info = self.client.get_repo(owner, repo)
        if not repo_info:
            logger.error("Repository %s/%s not found", owner, repo)
            return []

        stars = repo_info.get("stargazers_count", 0)
        size_mb = repo_info.get("size", 0) / 1024.0
        logger.info("Analyzing %s/%s (stars=%d, size=%.1fMB)", owner, repo, stars, size_mb)

        max_pages = max(1, (max_issues + 99) // 100)
        issues = self.client.get_closed_issues(owner, repo, max_pages=max_pages)
        logger.info("Found %d closed issues/PRs", len(issues))

        results = []
        analyzed = 0
        for issue_data in issues:
            if issue_data.get("pull_request"):
                continue
            if analyzed >= max_issues:
                break
            analyzed += 1

            issue_number = issue_data["number"]
            logger.info("Analyzing issue #%d: %s", issue_number, issue_data.get("title", "")[:60])

            analysis = self.analyze_issue(
                owner, repo, issue_number,
                repo_info=repo_info, issue_data=issue_data, early_reject=True,
            )
            results.append(analysis)

            if analysis.meets_criteria:
                logger.info("  PASS Meets criteria (score=%.1f)", analysis.complexity_score)
            else:
                reasons = "; ".join(analysis.rejection_reasons[:2])
                logger.debug("  FAIL Rejected: %s", reasons)

        results.sort(key=lambda a: (-int(a.meets_criteria), -a.complexity_score))
        return results

    def analyze_specific_issue(self, owner: str, repo: str,
                               issue_number: int) -> IssueAnalysis:
        """Analyze a single specific issue in detail (no early rejection)."""
        return self.analyze_issue(owner, repo, issue_number, early_reject=False)
