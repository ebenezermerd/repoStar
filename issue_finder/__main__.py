"""Allow running as python -m issue_finder."""

from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
