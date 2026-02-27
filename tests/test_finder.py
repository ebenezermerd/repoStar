from __future__ import annotations

import unittest

from issue_finder.finder import FinderConfig, IssueFinder
from issue_finder.github_client import GitHubClient


class FinderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.finder = IssueFinder(GitHubClient(), FinderConfig())

    def test_extract_linked_pull_numbers_from_source_issue(self) -> None:
        timeline = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 123,
                        "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/123"},
                    }
                },
            },
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 123,
                        "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/123"},
                    }
                },
            },
        ]
        self.assertEqual(self.finder._extract_linked_pull_numbers(timeline), [123])

    def test_extract_linked_pull_numbers_from_subject_url(self) -> None:
        timeline = [
            {
                "event": "connected",
                "subject": {
                    "type": "PullRequest",
                    "url": "https://api.github.com/repos/org/repo/pulls/44",
                },
            }
        ]
        self.assertEqual(self.finder._extract_linked_pull_numbers(timeline), [44])


if __name__ == "__main__":
    unittest.main()
