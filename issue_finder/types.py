from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class PullFile:
    filename: str
    status: str
    additions: int
    deletions: int
    changes: int


@dataclass(frozen=True)
class Candidate:
    repo_full_name: str
    repo_url: str
    repo_stars: int
    repo_size_mb: float
    issue_number: int
    issue_title: str
    issue_url: str
    issue_body_len: int
    pr_number: int
    pr_url: str
    base_sha: str
    merge_commit_sha: str | None
    changed_py_files: int
    changed_non_test_doc_py_files: int
    total_changes_non_test_doc_py: int
    max_file_changes_non_test_doc_py: int
    top_changed_files: tuple[str, ...]
    score: float
    reasons: tuple[str, ...]

