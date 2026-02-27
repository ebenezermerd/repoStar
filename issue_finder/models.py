from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class RepositorySnapshot:
    owner: str
    name: str
    stars: int
    size_mb: float
    html_url: str
    default_branch: str
    description: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class PullRequestSnapshot:
    number: int
    html_url: str
    title: str
    base_sha: str
    additions: int
    deletions: int
    commits: int
    changed_files: int
    merged_at: str | None


@dataclass(frozen=True)
class FileChangeSummary:
    path: str
    additions: int
    deletions: int
    changes: int


@dataclass(frozen=True)
class ComplexityBreakdown:
    score: float
    label: str
    files_component: float
    changes_component: float
    commits_component: float
    max_file_component: float
    issue_body_component: float


@dataclass(frozen=True)
class IssueCandidate:
    repository: RepositorySnapshot
    issue_number: int
    issue_title: str
    issue_url: str
    issue_body_length: int
    pull_request: PullRequestSnapshot
    python_non_test_files: list[FileChangeSummary]
    max_python_file_change: FileChangeSummary
    total_python_changes: int
    complexity: ComplexityBreakdown
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["repository"]["full_name"] = self.repository.full_name
        return payload
