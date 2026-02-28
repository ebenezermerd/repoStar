"""Issue analysis for PR Writer HFI project criteria."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import (
    DOC_FILE_PATTERNS,
    MIN_PYTHON_FILES_CHANGED,
    MIN_SUBSTANTIAL_CHANGES_IN_FILE,
    TEST_FILE_PATTERNS,
    URL_PATTERN,
)
from .github_client import GitHubClient, IssueInfo, PRFileChange, PRAnalysis


def _is_test_file(filename: str) -> bool:
    """Check if file is a test file."""
    fn = filename.lower()
    return any(p in fn for p in TEST_FILE_PATTERNS)


def _is_doc_file(filename: str) -> bool:
    """Check if file is documentation."""
    fn = filename.lower()
    return any(p in fn for p in DOC_FILE_PATTERNS)


def _is_code_python_file(filename: str) -> bool:
    """Check if file is a Python code file (not test, not doc)."""
    if not filename.endswith(".py"):
        return False
    if _is_test_file(filename):
        return False
    if _is_doc_file(filename):
        return False
    return True


def _body_has_links_or_images(body: str | None) -> bool:
    """Check if issue body contains URLs or markdown images."""
    if not body or not body.strip():
        return False
    return bool(URL_PATTERN.search(body))


def _body_is_pure_text(body: str | None) -> bool:
    """Issue description should be pure - no images, no links."""
    return not _body_has_links_or_images(body)


def _has_substantial_changes(files: list[PRFileChange], min_changes: int = 5) -> bool:
    """At least one code Python file should have substantial changes."""
    for f in files:
        if not _is_code_python_file(f.filename):
            continue
        total = f.additions + f.deletions
        if total >= min_changes:
            return True
    return False


def _count_code_python_files(files: list[PRFileChange]) -> int:
    """Count Python code files changed (excluding test and doc)."""
    return sum(1 for f in files if _is_code_python_file(f.filename))


@dataclass
class IssueAnalysisResult:
    """Result of issue analysis."""

    issue: IssueInfo
    pr_analysis: PRAnalysis | None
    passes: bool
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)
    score: float = 0.0
    complexity_hint: str = ""

    @property
    def summary(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "OK"


class IssueAnalyzer:
    """Analyzes issues against PR Writer criteria."""

    def __init__(self, client: GitHubClient):
        self.client = client

    def analyze_issue(
        self, full_name: str, issue: IssueInfo, analyze_pr: bool = True
    ) -> IssueAnalysisResult:
        """Analyze an issue against all PR Writer criteria."""
        reasons = []
        details = {}
        score = 0.0

        # 1. Issue must be closed
        if issue.state != "closed":
            return IssueAnalysisResult(
                issue=issue,
                pr_analysis=None,
                passes=False,
                reasons=["Issue is not closed"],
            )

        # 2. Body must be pure - no images, no links
        if _body_has_links_or_images(issue.body):
            reasons.append("Issue body contains links or images (must be pure text)")
        else:
            score += 2.0
            reasons.append("Body is pure text")

        # 3. Find linked PR
        prs = self.client.get_prs_linked_to_issue(full_name, issue.number)
        if not prs:
            reasons.append("No PR found that references this issue")
            return IssueAnalysisResult(
                issue=issue,
                pr_analysis=None,
                passes=False,
                reasons=reasons,
                details=details,
                score=score,
            )

        # 4. One-way link: PR should close only this issue
        best_pr = None
        best_pr_analysis = None

        for pr in prs:
            body = self.client.get_pr_body(full_name, pr.number)
            closes = GitHubClient.parse_closes_keywords(body, issue.number)
            if issue.number not in closes:
                continue
            if len(closes) > 1:
                reasons.append(f"PR closes multiple issues: {closes}")
                continue
            files = self.client.get_pr_files(full_name, pr.number)
            base_sha = self.client.get_pr_base_sha(full_name, pr.number)
            pr_analysis = PRAnalysis(
                number=pr.number,
                html_url=pr.html_url,
                state=pr.state,
                merged=pr.merged,
                body=body,
                files=files,
                closes_issues=closes,
                base_sha=base_sha,
            )
            best_pr = pr
            best_pr_analysis = pr_analysis
            break

        if not best_pr_analysis:
            reasons.append("No PR with one-way close (closes only this issue)")
            return IssueAnalysisResult(
                issue=issue,
                pr_analysis=None,
                passes=False,
                reasons=reasons,
                details=details,
                score=score,
            )

        # 5. At least 4 Python code files changed (excluding test/docs)
        code_files = _count_code_python_files(best_pr_analysis.files)
        details["code_python_files_changed"] = code_files
        if code_files < MIN_PYTHON_FILES_CHANGED:
            reasons.append(
                f"Only {code_files} Python code files changed (need >= {MIN_PYTHON_FILES_CHANGED})"
            )
        else:
            score += 3.0
            reasons.append(f"{code_files} Python code files changed")

        # 6. At least one code file with substantial changes
        if not _has_substantial_changes(
            best_pr_analysis.files, MIN_SUBSTANTIAL_CHANGES_IN_FILE
        ):
            reasons.append(
                f"No code file has >= {MIN_SUBSTANTIAL_CHANGES_IN_FILE} lines changed"
            )
        else:
            score += 2.0
            reasons.append("At least one code file has substantial changes")

        # Complexity hint based on changes
        total_additions = sum(f.additions for f in best_pr_analysis.files)
        total_deletions = sum(f.deletions for f in best_pr_analysis.files)
        details["total_additions"] = total_additions
        details["total_deletions"] = total_deletions

        if total_additions + total_deletions > 100:
            complexity_hint = "High complexity"
        elif total_additions + total_deletions > 50:
            complexity_hint = "Medium-high complexity"
        elif total_additions + total_deletions > 20:
            complexity_hint = "Medium complexity"
        else:
            complexity_hint = "May be too simple (model might solve in 1-2 turns)"

        # Well-scoped check: title length and body length
        if len(issue.title) < 10:
            reasons.append("Issue title may be too vague")
        else:
            score += 0.5

        if issue.body and len(issue.body) > 50:
            score += 0.5
            reasons.append("Issue has substantive description")
        elif not issue.body or len(issue.body) < 20:
            reasons.append("Issue description may be too brief")

        passes = (
            code_files >= MIN_PYTHON_FILES_CHANGED
            and _has_substantial_changes(
                best_pr_analysis.files, MIN_SUBSTANTIAL_CHANGES_IN_FILE
            )
        )

        return IssueAnalysisResult(
            issue=issue,
            pr_analysis=best_pr_analysis,
            passes=passes,
            reasons=reasons,
            details=details,
            score=score,
            complexity_hint=complexity_hint,
        )
