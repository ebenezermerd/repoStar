from __future__ import annotations

from .models import ComplexityBreakdown


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def score_complexity(
    python_non_test_files_changed: int,
    total_python_changes: int,
    pr_commits: int,
    max_single_python_file_changes: int,
    issue_body_length: int,
) -> ComplexityBreakdown:
    files_component = _clamp(python_non_test_files_changed * 6.0, 0.0, 30.0)
    changes_component = _clamp(total_python_changes / 20.0, 0.0, 25.0)
    commits_component = _clamp(pr_commits * 2.5, 0.0, 15.0)
    max_file_component = _clamp(max_single_python_file_changes / 6.0, 0.0, 20.0)
    issue_body_component = _clamp(issue_body_length / 120.0, 0.0, 10.0)

    total_score = round(
        files_component
        + changes_component
        + commits_component
        + max_file_component
        + issue_body_component,
        2,
    )

    if total_score >= 75:
        label = "high"
    elif total_score >= 50:
        label = "medium"
    else:
        label = "low"

    return ComplexityBreakdown(
        score=total_score,
        label=label,
        files_component=round(files_component, 2),
        changes_component=round(changes_component, 2),
        commits_component=round(commits_component, 2),
        max_file_component=round(max_file_component, 2),
        issue_body_component=round(issue_body_component, 2),
    )
