"""Scoring profiles for issue analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


PROFILES_DIR = Path.home() / ".issue_finder" / "profiles"


@dataclass
class ScoringProfile:
    """Configurable scoring criteria for issue analysis."""

    name: str
    description: str = ""

    # Repository gates
    min_stars: int = 200
    max_size_mb: int = 200
    required_language: str = "Python"

    # Issue gates
    require_closed: bool = True
    require_pure_body: bool = True
    require_one_way_close: bool = True

    # File change thresholds
    min_code_files_changed: int = 4
    min_substantial_changes: int = 5

    # Scoring weights
    pure_body_score: float = 2.0
    code_files_score: float = 3.0
    substantial_changes_score: float = 2.0
    good_title_score: float = 0.5
    good_description_score: float = 0.5

    # Minimum total score to pass
    min_score: float = 5.0

    # File patterns
    test_patterns: tuple[str, ...] = (
        "test_", "_test", "tests/", "/test/", "conftest.py",
        "unittest", "pytest", "spec.py",
    )
    doc_patterns: tuple[str, ...] = (
        "readme", "changelog", "docs/", ".md", ".rst", ".txt",
        "license", "contributing", "setup.cfg", "pyproject.toml",
    )

    # Label-based pre-filtering
    preferred_labels: list[str] = field(default_factory=lambda: [
        "bug", "enhancement", "feature", "refactor", "improvement",
    ])
    skip_labels: list[str] = field(default_factory=lambda: [
        "duplicate", "wontfix", "invalid", "question", "documentation",
        "dependencies", "stale",
    ])

    def to_dict(self) -> dict:
        d = {}
        for k, v in self.__dict__.items():
            if isinstance(v, tuple):
                d[k] = list(v)
            else:
                d[k] = v
        return d

    def save(self, path: Path | None = None) -> Path:
        path = path or PROFILES_DIR / f"{self.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path


# Built-in profiles
PR_WRITER_PROFILE = ScoringProfile(
    name="pr_writer",
    description="PR Writer HFI project criteria (default)",
)

GENERAL_PROFILE = ScoringProfile(
    name="general",
    description="General-purpose issue discovery (relaxed criteria)",
    min_stars=50,
    max_size_mb=500,
    require_pure_body=False,
    require_one_way_close=False,
    min_code_files_changed=2,
    min_substantial_changes=3,
    min_score=3.0,
)

BUILTIN_PROFILES: dict[str, ScoringProfile] = {
    "pr_writer": PR_WRITER_PROFILE,
    "general": GENERAL_PROFILE,
}


def load_profile(name_or_path: str) -> ScoringProfile:
    """Load a profile by built-in name or JSON file path."""
    if name_or_path in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[name_or_path]

    path = Path(name_or_path)
    if not path.exists():
        path = PROFILES_DIR / f"{name_or_path}.json"

    if path.exists():
        data = json.loads(path.read_text())
        for k in ("test_patterns", "doc_patterns"):
            if k in data and isinstance(data[k], list):
                data[k] = tuple(data[k])
        return ScoringProfile(**data)

    raise ValueError(f"Unknown profile: {name_or_path}")


def list_profiles() -> list[ScoringProfile]:
    """List all available profiles (built-in + custom)."""
    profiles = list(BUILTIN_PROFILES.values())
    if PROFILES_DIR.exists():
        for f in PROFILES_DIR.glob("*.json"):
            try:
                p = load_profile(str(f))
                if p.name not in BUILTIN_PROFILES:
                    profiles.append(p)
            except Exception:
                pass
    return profiles
