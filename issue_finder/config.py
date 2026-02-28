"""Configuration constants for the Issue Finder."""

import re

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
URL_PATTERN = re.compile(
    r'https?://[^\s\)\]\>]+|'
    r'\[.*?\]\(https?://[^\)]+\)|'
    r'!\[.*?\]\([^\)]+\)'  # Markdown images
)

# ── Pre-filtering: noise patterns in issue titles ────────────
NOISE_TITLE_PATTERNS = (
    "bump", "update depend", "changelog", "release v", "release:",
    "chore:", "ci:", "docs:", "typo", "readme",
    "merge branch", "merge pull", "version bump",
    "upgrade to", "pin depend", "renovate",
)

# ── Auto-discovery: curated Python repos ─────────────────────
CURATED_PYTHON_REPOS = [
    "tiangolo/fastapi", "pallets/flask", "django/django",
    "encode/httpx", "pydantic/pydantic", "Textualize/rich",
    "Textualize/textual", "tiangolo/typer", "pallets/click",
    "celery/celery", "psf/requests", "aio-libs/aiohttp",
    "pytest-dev/pytest", "astral-sh/ruff", "psf/black",
    "python/mypy", "sqlalchemy/sqlalchemy", "encode/starlette",
    "marshmallow-code/marshmallow", "jazzband/pip-tools",
    "pallets/jinja", "pallets/werkzeug", "mitmproxy/mitmproxy",
    "python-poetry/poetry", "pypa/pip", "pypa/setuptools",
    "boto/boto3", "fabric/fabric", "paramiko/paramiko",
    "arrow-py/arrow", "dateutil/dateutil",
    "tqdm/tqdm", "Delgan/loguru", "cool-RR/PySnooper",
    "dbader/schedule", "agronholm/anyio", "encode/uvicorn",
    "scrapy/scrapy", "psf/httptools", "ijl/orjson",
    "samuelcolvin/watchfiles", "jpadilla/pyjwt",
]

# ── Auto-discovery: topic keywords to search ─────────────────
DISCOVERY_TOPICS = (
    "web", "cli", "api", "data", "automation",
    "devtools", "testing", "async", "http", "database",
    "security", "networking", "parsing", "validation",
)
