"""Configuration constants for the Issue Finder."""

# Repository criteria (from PR Writer guidelines)
REPO_MAX_SIZE_MB = 200
REPO_MIN_STARS = 200
REPO_SIZE_KB = REPO_MAX_SIZE_MB * 1024

# Issue criteria
MIN_PYTHON_FILES_CHANGED = 4  # Excluding test and documentation files
MIN_SUBSTANTIAL_CHANGES_IN_FILE = 5  # Lines changed in at least one non-test file

# File patterns to exclude from "code files" count (tests, docs)
TEST_FILE_PATTERNS = (
    "test_", "_test", "tests/", "/test/", "conftest.py",
    "unittest", "pytest", "spec.py"
)
DOC_FILE_PATTERNS = (
    "readme", "changelog", "docs/", ".md", ".rst", ".txt",
    "license", "contributing", "setup.cfg", "pyproject.toml"
)

# GitHub search exclusions (from guidelines)
GITHUB_SEARCH_EXCLUSIONS = ["collection", "list", "guide", "projects", "exercises"]

# URL regex for detecting links in issue body
import re
URL_PATTERN = re.compile(
    r'https?://[^\s\)\]\>]+|'
    r'\[.*?\]\(https?://[^\)]+\)|'
    r'!\[.*?\]\([^\)]+\)'  # Markdown images
)
