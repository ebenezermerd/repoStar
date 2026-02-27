from __future__ import annotations

import re


_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def looks_like_pure_text(
    title: str,
    body: str | None,
    *,
    reject_screenshot_mentions: bool = True,
) -> tuple[bool, tuple[str, ...]]:
    """
    Heuristic gate: reject issues that contain links or images.
    This intentionally errs on the side of excluding candidates.
    """
    reasons: list[str] = []
    hay = f"{title}\n{body or ''}"

    if _MD_IMAGE_RE.search(hay):
        reasons.append("issue_contains_markdown_image")
    if _MD_LINK_RE.search(hay):
        reasons.append("issue_contains_markdown_link")
    if _URL_RE.search(hay):
        reasons.append("issue_contains_url")

    if reject_screenshot_mentions:
        # Common screenshot/reference patterns even without explicit URLs.
        if "screenshot" in hay.lower():
            reasons.append("issue_mentions_screenshot")

    return (len(reasons) == 0), tuple(reasons)

