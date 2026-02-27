from __future__ import annotations

import re
from collections.abc import Iterable

URL_PATTERN = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
IMAGE_PATTERN = re.compile(r"!\[[^\]]*]\(([^)]+)\)|<img\b", re.IGNORECASE)
REFERENCE_LINK_PATTERN = re.compile(r"^\[[^\]]+]:\s+\S+", re.MULTILINE)

CLOSING_REFERENCE_PATTERN = re.compile(
    r"""(?ix)
    \b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*
    (?:
      https?://github\.com/
      (?P<owner>[\w.-]+)/
      (?P<repo>[\w.-]+)/
      issues/(?P<url_number>\d+)
      |
      \#(?P<short_number>\d+)
    )
    """
)


def contains_links_or_images(text: str | None) -> bool:
    if not text:
        return False

    return bool(
        URL_PATTERN.search(text)
        or MARKDOWN_LINK_PATTERN.search(text)
        or IMAGE_PATTERN.search(text)
        or REFERENCE_LINK_PATTERN.search(text)
    )


def is_python_non_test_non_doc_file(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    base_name = normalized.rsplit("/", maxsplit=1)[-1]

    if not normalized.endswith(".py"):
        return False

    if any(marker in normalized for marker in ("/docs/", "/doc/", "/documentation/")):
        return False

    if normalized.startswith(("docs/", "doc/", "documentation/")):
        return False

    if "/tests/" in normalized or normalized.startswith(("tests/", "test/")):
        return False

    if "/test/" in normalized:
        return False

    if base_name.startswith("test_") or base_name.endswith("_test.py"):
        return False

    return True


def extract_closing_issue_numbers(pr_body: str | None, owner: str, repo: str) -> list[int]:
    if not pr_body:
        return []

    owner_lower = owner.lower()
    repo_lower = repo.lower()
    seen: set[int] = set()
    ordered: list[int] = []

    for match in CLOSING_REFERENCE_PATTERN.finditer(pr_body):
        number = match.group("short_number") or match.group("url_number")
        if not number:
            continue

        matched_owner = match.group("owner")
        matched_repo = match.group("repo")
        if matched_owner and matched_repo:
            if matched_owner.lower() != owner_lower or matched_repo.lower() != repo_lower:
                continue

        issue_number = int(number)
        if issue_number in seen:
            continue
        seen.add(issue_number)
        ordered.append(issue_number)

    return ordered


def dedupe_preserve_order(values: Iterable[int]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
