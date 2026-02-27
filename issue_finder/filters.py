"""Filtering logic for issues, PRs, and file changes."""

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

IMAGE_PATTERN = re.compile(
    r'!\[.*?\]\(.*?\)'           # markdown images ![alt](url)
    r'|<img\s[^>]*>'            # HTML img tags
    r'|https?://\S+\.(?:png|jpg|jpeg|gif|svg|webp|bmp|ico)\b',  # direct image URLs
    re.IGNORECASE,
)

LINK_PATTERN = re.compile(
    r'https?://\S+'              # any HTTP(S) URL
    r'|\[.*?\]\(https?://.*?\)', # markdown links [text](url)
    re.IGNORECASE,
)

TRIVIAL_LINK_PATTERN = re.compile(
    r'https?://\S+',
    re.IGNORECASE,
)

TEST_FILE_PATTERNS = [
    r'(?:^|/)tests?/',
    r'(?:^|/)test_[^/]+\.py$',
    r'(?:^|/)[^/]*_test\.py$',
    r'(?:^|/)conftest\.py$',
    r'(?:^|/)testing/',
    r'(?:^|/)fixtures/',
]

DOC_FILE_PATTERNS = [
    r'(?:^|/)docs?/',
    r'(?:^|/)documentation/',
    r'\.(?:md|rst|txt|adoc)$',
    r'(?:^|/)README',
    r'(?:^|/)CHANGELOG',
    r'(?:^|/)CONTRIBUTING',
    r'(?:^|/)LICENSE',
    r'(?:^|/)HISTORY',
    r'(?:^|/)AUTHORS',
]

CONFIG_FILE_PATTERNS = [
    r'\.(?:cfg|ini|toml|yaml|yml|json)$',
    r'(?:^|/)setup\.py$',
    r'(?:^|/)pyproject\.toml$',
    r'(?:^|/)Makefile$',
    r'(?:^|/)Dockerfile',
    r'(?:^|/)\.github/',
    r'(?:^|/)tox\.ini$',
]


@dataclass
class FileChangeInfo:
    filename: str
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    status: str = ""
    is_python: bool = False
    is_test: bool = False
    is_doc: bool = False
    is_config: bool = False

    @property
    def is_code_python(self) -> bool:
        return self.is_python and not self.is_test and not self.is_doc and not self.is_config

    @property
    def total_changes(self) -> int:
        return self.additions + self.deletions


@dataclass
class IssueAnalysis:
    owner: str
    repo: str
    issue_number: int
    issue_title: str = ""
    issue_url: str = ""
    issue_body: str = ""
    pr_number: Optional[int] = None
    pr_url: str = ""
    pr_merged: bool = False
    base_sha: str = ""
    repo_stars: int = 0
    repo_size_mb: float = 0.0

    has_images: bool = False
    has_links: bool = False
    link_count: int = 0
    is_pure_text: bool = True

    linked_pr_count: int = 0
    file_changes: list = field(default_factory=list)

    total_python_code_files: int = 0
    total_test_files_changed: int = 0
    total_doc_files_changed: int = 0
    max_file_changes: int = 0
    max_change_file: str = ""
    total_additions: int = 0
    total_deletions: int = 0

    complexity_score: float = 0.0
    meets_criteria: bool = False
    rejection_reasons: list = field(default_factory=list)

    @property
    def issue_body_length(self) -> int:
        return len(self.issue_body) if self.issue_body else 0

    def to_dict(self) -> dict:
        return {
            "owner": self.owner,
            "repo": self.repo,
            "issue_number": self.issue_number,
            "issue_title": self.issue_title,
            "issue_url": self.issue_url,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "pr_merged": self.pr_merged,
            "base_sha": self.base_sha,
            "repo_stars": self.repo_stars,
            "repo_size_mb": round(self.repo_size_mb, 2),
            "is_pure_text": self.is_pure_text,
            "has_images": self.has_images,
            "has_links": self.has_links,
            "link_count": self.link_count,
            "linked_pr_count": self.linked_pr_count,
            "total_python_code_files": self.total_python_code_files,
            "total_test_files_changed": self.total_test_files_changed,
            "total_doc_files_changed": self.total_doc_files_changed,
            "max_file_changes": self.max_file_changes,
            "max_change_file": self.max_change_file,
            "total_additions": self.total_additions,
            "total_deletions": self.total_deletions,
            "complexity_score": round(self.complexity_score, 2),
            "meets_criteria": self.meets_criteria,
            "rejection_reasons": self.rejection_reasons,
        }


def classify_file(filename: str) -> FileChangeInfo:
    """Classify a file based on its path and extension."""
    info = FileChangeInfo(filename=filename)
    info.is_python = filename.endswith(".py")

    for pattern in TEST_FILE_PATTERNS:
        if re.search(pattern, filename, re.IGNORECASE):
            info.is_test = True
            break

    for pattern in DOC_FILE_PATTERNS:
        if re.search(pattern, filename, re.IGNORECASE):
            info.is_doc = True
            break

    for pattern in CONFIG_FILE_PATTERNS:
        if re.search(pattern, filename, re.IGNORECASE):
            info.is_config = True
            break

    return info


def check_issue_text_purity(body: str) -> tuple[bool, bool, int]:
    """Check if issue body is pure text (no images or links).

    Returns (has_images, has_links, link_count).
    """
    if not body:
        return False, False, 0

    has_images = bool(IMAGE_PATTERN.search(body))
    links = TRIVIAL_LINK_PATTERN.findall(body)
    has_links = len(links) > 0

    return has_images, has_links, len(links)


def analyze_file_changes(pr_files: list[dict]) -> list[FileChangeInfo]:
    """Analyze PR file changes and classify each file."""
    results = []
    for f in pr_files:
        info = classify_file(f.get("filename", ""))
        info.additions = f.get("additions", 0)
        info.deletions = f.get("deletions", 0)
        info.changes = f.get("changes", 0)
        info.status = f.get("status", "")
        results.append(info)
    return results


def find_linked_prs_from_timeline(timeline_events: list) -> list[int]:
    """Extract PR numbers that were cross-referenced from timeline events."""
    pr_numbers = set()
    for event in timeline_events:
        if not isinstance(event, dict):
            continue

        event_type = event.get("event")

        if event_type == "cross-referenced":
            source = event.get("source", {})
            issue_data = source.get("issue", {})
            pr_data = issue_data.get("pull_request", {})
            if pr_data and issue_data.get("state") == "closed":
                pr_url = pr_data.get("html_url", "") or issue_data.get("html_url", "")
                if pr_url:
                    try:
                        pr_num = int(pr_url.rstrip("/").split("/")[-1])
                        pr_numbers.add(pr_num)
                    except (ValueError, IndexError):
                        pass

        elif event_type == "connected" or event_type == "closed":
            commit_id = event.get("commit_id")
            if event.get("source", {}).get("issue", {}).get("pull_request"):
                source_issue = event["source"]["issue"]
                try:
                    pr_num = int(source_issue["html_url"].rstrip("/").split("/")[-1])
                    pr_numbers.add(pr_num)
                except (ValueError, IndexError, KeyError):
                    pass

    return sorted(pr_numbers)


def find_linked_prs_from_events(events: list) -> list[int]:
    """Extract PR numbers from issue events (closed via commit/PR)."""
    pr_numbers = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("event") == "closed" and event.get("commit_id"):
            pass
        if event.get("event") == "referenced":
            pass
    return sorted(pr_numbers)


def find_closing_pr_from_body(issue_body: str, pr_list: list[dict],
                               owner: str, repo: str) -> Optional[int]:
    """Look for PR references in issue body text (less reliable fallback)."""
    if not issue_body:
        return None
    pr_ref_pattern = re.compile(
        rf'(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*#(\d+)',
        re.IGNORECASE
    )
    matches = pr_ref_pattern.findall(issue_body)
    for match in matches:
        pr_num = int(match)
        for pr in pr_list:
            if pr.get("number") == pr_num:
                return pr_num
    return None
