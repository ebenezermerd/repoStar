from __future__ import annotations

import unittest

from issue_finder.scoring import score_complexity


class ScoringTest(unittest.TestCase):
    def test_score_label_boundaries(self) -> None:
        low = score_complexity(
            python_non_test_files_changed=4,
            total_python_changes=30,
            pr_commits=1,
            max_single_python_file_changes=20,
            issue_body_length=80,
        )
        self.assertEqual(low.label, "low")

        medium = score_complexity(
            python_non_test_files_changed=6,
            total_python_changes=320,
            pr_commits=3,
            max_single_python_file_changes=120,
            issue_body_length=260,
        )
        self.assertEqual(medium.label, "medium")

        high = score_complexity(
            python_non_test_files_changed=12,
            total_python_changes=2000,
            pr_commits=10,
            max_single_python_file_changes=500,
            issue_body_length=1400,
        )
        self.assertEqual(high.label, "high")

    def test_score_increases_with_more_changes(self) -> None:
        smaller = score_complexity(
            python_non_test_files_changed=4,
            total_python_changes=120,
            pr_commits=2,
            max_single_python_file_changes=60,
            issue_body_length=150,
        )
        larger = score_complexity(
            python_non_test_files_changed=7,
            total_python_changes=500,
            pr_commits=5,
            max_single_python_file_changes=200,
            issue_body_length=450,
        )
        self.assertGreater(larger.score, smaller.score)


if __name__ == "__main__":
    unittest.main()
