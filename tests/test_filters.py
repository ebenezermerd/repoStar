from __future__ import annotations

import unittest

from issue_finder.filters import (
    contains_links_or_images,
    extract_closing_issue_numbers,
    is_python_non_test_non_doc_file,
)


class FiltersTest(unittest.TestCase):
    def test_contains_links_or_images(self) -> None:
        self.assertTrue(contains_links_or_images("Please see https://example.com"))
        self.assertTrue(contains_links_or_images("![diagram](http://img.com/a.png)"))
        self.assertTrue(contains_links_or_images("[link](https://github.com)"))
        self.assertFalse(
            contains_links_or_images("This is a plain issue description without url")
        )

    def test_extract_closing_issue_numbers(self) -> None:
        body = """
        Fixes #12
        closes https://github.com/example/project/issues/99
        resolves https://github.com/other/project/issues/5
        """
        self.assertEqual(
            extract_closing_issue_numbers(body, owner="example", repo="project"),
            [12, 99],
        )

    def test_is_python_non_test_non_doc_file(self) -> None:
        self.assertTrue(is_python_non_test_non_doc_file("src/core/runner.py"))
        self.assertFalse(is_python_non_test_non_doc_file("tests/test_runner.py"))
        self.assertFalse(is_python_non_test_non_doc_file("docs/config.py"))
        self.assertFalse(is_python_non_test_non_doc_file("README.md"))


if __name__ == "__main__":
    unittest.main()
