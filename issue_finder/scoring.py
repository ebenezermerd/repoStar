from __future__ import annotations

from dataclasses import dataclass

from .types import PullFile


@dataclass(frozen=True)
class PrChangeStats:
    changed_py_files: int
    changed_non_test_doc_py_files: int
    total_changes_non_test_doc_py: int
    max_file_changes_non_test_doc_py: int
    top_changed_files: tuple[str, ...]


def compute_change_stats(
    *,
    pr_files: list[PullFile],
    is_non_test_non_doc_python: callable,
    top_n: int = 5,
) -> PrChangeStats:
    changed_py = [f for f in pr_files if f.filename.lower().endswith(".py")]
    changed_non_test_doc = [f for f in pr_files if is_non_test_non_doc_python(f.filename)]

    total_changes = sum(f.changes for f in changed_non_test_doc)
    max_changes = max((f.changes for f in changed_non_test_doc), default=0)

    top = sorted(changed_non_test_doc, key=lambda f: f.changes, reverse=True)[:top_n]
    top_files = tuple(f"{f.filename} ({f.changes})" for f in top)

    return PrChangeStats(
        changed_py_files=len(changed_py),
        changed_non_test_doc_py_files=len(changed_non_test_doc),
        total_changes_non_test_doc_py=total_changes,
        max_file_changes_non_test_doc_py=max_changes,
        top_changed_files=top_files,
    )


def score_candidate(
    *,
    stars: int,
    size_mb: float,
    issue_body_len: int,
    stats: PrChangeStats,
) -> float:
    """
    Score is a heuristic to surface "medium-complex" issues:
    - reward more non-test python files changed
    - reward higher changes concentration in at least one file
    - slightly reward longer issue text (up to a point)
    - slight reward for stars (quality proxy), penalize big repos near limit
    """
    file_score = min(stats.changed_non_test_doc_py_files, 12) * 10.0
    churn_score = min(stats.total_changes_non_test_doc_py, 1200) / 10.0
    concentration_score = min(stats.max_file_changes_non_test_doc_py, 500) / 5.0

    issue_score = min(max(issue_body_len, 0), 4000) / 200.0
    stars_score = min(stars, 50_000) ** 0.5  # diminishing returns

    # Prefer repos comfortably below 200MB (soft penalty above 120MB).
    size_penalty = 0.0
    if size_mb > 120:
        size_penalty = (size_mb - 120) * 1.5

    return file_score + churn_score + concentration_score + issue_score + (stars_score / 3.0) - size_penalty

