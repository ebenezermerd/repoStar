"""GitHub Issue Finder for PR Writer Project.

Finds GitHub issues that match specific criteria for AI training annotation work:
- Python repositories with 200+ stars and <200MB size
- Closed issues with pure text descriptions (no images/links)
- Issues linked to exactly one merged PR (one-way relationship)
- PRs that change at least 4 non-test, non-doc Python files
- At least one file with substantial changes
"""

__version__ = "1.0.0"
