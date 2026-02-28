"""Disk-based TTL cache for GitHub API responses."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path


CACHE_DIR = Path.home() / ".issue_finder" / "cache"

# Default TTLs in seconds
TTL_SEARCH = 3600       # 1 hour for search results
TTL_ISSUES = 86400      # 24 hours for issue/PR data
TTL_REPO = 86400        # 24 hours for repo info
TTL_TRENDING = 7200     # 2 hours for trending


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class CacheStore:
    """Simple disk-based JSON cache with TTL expiry."""

    def __init__(self, base_dir: Path | None = None, enabled: bool = True):
        self.base_dir = base_dir or CACHE_DIR
        self.enabled = enabled
        self._hits = 0
        self._misses = 0
        if enabled:
            self.base_dir.mkdir(parents=True, exist_ok=True)

    async def get(self, namespace: str, key: str) -> dict | list | None:
        if not self.enabled:
            return None
        path = self._path(namespace, key)
        if not path.exists():
            self._misses += 1
            return None
        try:
            entry = json.loads(path.read_text())
            if time.time() > entry.get("expires_at", 0):
                path.unlink(missing_ok=True)
                self._misses += 1
                return None
            self._hits += 1
            return entry["data"]
        except (json.JSONDecodeError, KeyError):
            path.unlink(missing_ok=True)
            self._misses += 1
            return None

    async def set(self, namespace: str, key: str, data, ttl: int = TTL_SEARCH):
        if not self.enabled:
            return
        path = self._path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "key": key,
            "expires_at": time.time() + ttl,
            "data": data,
        }
        path.write_text(json.dumps(entry, default=str))

    def invalidate(self, namespace: str | None = None):
        if namespace:
            ns_dir = self.base_dir / namespace
            if ns_dir.exists():
                for f in ns_dir.glob("*.json"):
                    f.unlink(missing_ok=True)
        else:
            for ns_dir in self.base_dir.iterdir():
                if ns_dir.is_dir():
                    for f in ns_dir.glob("*.json"):
                        f.unlink(missing_ok=True)

    def stats(self) -> dict:
        total_files = 0
        total_bytes = 0
        expired = 0
        now = time.time()
        for f in self.base_dir.rglob("*.json"):
            total_files += 1
            total_bytes += f.stat().st_size
            try:
                entry = json.loads(f.read_text())
                if now > entry.get("expires_at", 0):
                    expired += 1
            except Exception:
                expired += 1
        return {
            "entries": total_files,
            "expired": expired,
            "size_kb": round(total_bytes / 1024, 1),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{self._hits / max(1, self._hits + self._misses) * 100:.0f}%",
        }

    def _path(self, namespace: str, key: str) -> Path:
        return self.base_dir / namespace / f"{_key_hash(key)}.json"
