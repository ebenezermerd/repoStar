"""Repository analysis for PR Writer compatibility."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import REPO_MAX_SIZE_MB, REPO_MIN_STARS, REPO_SIZE_KB
from .github_client import RepoInfo


@dataclass
class RepoAnalysisResult:
    """Result of repository analysis."""

    repo: RepoInfo
    passes: bool
    reasons: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def summary(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "OK"


def analyze_repo(repo: RepoInfo) -> RepoAnalysisResult:
    """Analyze if a repository meets PR Writer criteria."""
    reasons = []
    score = 0.0

    # Size: must be <= 200 MB
    if repo.size_kb > REPO_SIZE_KB:
        reasons.append(f"Size {repo.size_kb / 1024:.1f}MB > {REPO_MAX_SIZE_MB}MB")
    else:
        score += 2.0
        reasons.append(f"Size OK ({repo.size_kb / 1024:.1f}MB)")

    # Stars: >= 200
    if repo.stars < REPO_MIN_STARS:
        reasons.append(f"Stars {repo.stars} < {REPO_MIN_STARS}")
    else:
        score += 2.0
        if repo.stars >= 1000:
            score += 1.0
        reasons.append(f"Stars OK ({repo.stars})")

    # Language: Python
    if repo.language and repo.language.lower() != "python":
        reasons.append(f"Not primary Python (language: {repo.language})")
    else:
        score += 1.0
        reasons.append("Python repo")

    passes = repo.size_kb <= REPO_SIZE_KB and repo.stars >= REPO_MIN_STARS

    return RepoAnalysisResult(repo=repo, passes=passes, reasons=reasons, score=score)
