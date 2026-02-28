"""Persistent history tracking — remembers worked, skipped, and blocked issues."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = os.path.expanduser("~/.issue_finder_history.json")


@dataclass
class HistoryEntry:
    """Single tracked issue/repo."""
    key: str                       # "owner/repo#123" or "owner/repo"
    status: str                    # worked | skipped | blocked
    reason: str = ""
    timestamp: str = ""
    repo: str = ""
    issue_number: int = 0
    issue_title: str = ""
    score: float = 0.0
    pr_number: int = 0
    base_sha: str = ""


class HistoryStore:
    """Load / save / query a JSON history file."""

    def __init__(self, path: str | None = None):
        self.path = path or DEFAULT_PATH
        self.entries: dict[str, HistoryEntry] = {}
        self._load()

    # ── Persistence ─────────────────────────────────────────────

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                data = json.load(f)
            for key, obj in data.items():
                self.entries[key] = HistoryEntry(**obj)
        except Exception:
            pass

    def save(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(
                {k: asdict(v) for k, v in self.entries.items()},
                f, indent=2,
            )

    # ── Write operations ────────────────────────────────────────

    def mark(
        self,
        repo: str,
        issue_number: int,
        status: str,
        reason: str = "",
        issue_title: str = "",
        score: float = 0.0,
        pr_number: int = 0,
        base_sha: str = "",
    ):
        key = f"{repo}#{issue_number}"
        self.entries[key] = HistoryEntry(
            key=key,
            status=status,
            reason=reason,
            timestamp=datetime.now(timezone.utc).isoformat(),
            repo=repo,
            issue_number=issue_number,
            issue_title=issue_title,
            score=score,
            pr_number=pr_number,
            base_sha=base_sha,
        )
        self.save()

    def mark_repo(self, repo: str, status: str, reason: str = ""):
        key = repo
        self.entries[key] = HistoryEntry(
            key=key,
            status=status,
            reason=reason,
            timestamp=datetime.now(timezone.utc).isoformat(),
            repo=repo,
        )
        self.save()

    def remove(self, key: str):
        if key in self.entries:
            del self.entries[key]
            self.save()

    def clear_all(self):
        self.entries.clear()
        self.save()

    # ── Query operations ────────────────────────────────────────

    def is_blocked(self, repo: str, issue_number: int = 0) -> bool:
        if repo in self.entries and self.entries[repo].status == "blocked":
            return True
        if issue_number:
            key = f"{repo}#{issue_number}"
            return key in self.entries and self.entries[key].status == "blocked"
        return False

    def is_tracked(self, repo: str, issue_number: int = 0) -> str:
        """Return status if tracked, else empty string."""
        if issue_number:
            key = f"{repo}#{issue_number}"
            if key in self.entries:
                return self.entries[key].status
        if repo in self.entries:
            return self.entries[repo].status
        return ""

    def get_entry(self, key: str) -> HistoryEntry | None:
        return self.entries.get(key)

    def list_by_status(self, status: str) -> list[HistoryEntry]:
        return sorted(
            [e for e in self.entries.values() if e.status == status],
            key=lambda e: e.timestamp,
            reverse=True,
        )

    def all_entries(self) -> list[HistoryEntry]:
        return sorted(
            self.entries.values(),
            key=lambda e: e.timestamp,
            reverse=True,
        )

    def blocked_keys(self) -> set[str]:
        return {k for k, v in self.entries.items() if v.status == "blocked"}
