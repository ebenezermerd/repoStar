"""Tests for the filtering logic."""

import pytest

from issue_finder.filters import (
    FileChangeInfo,
    IssueAnalysis,
    classify_file,
    check_issue_text_purity,
    analyze_file_changes,
)


class TestClassifyFile:
    def test_python_file(self):
        info = classify_file("src/module/handler.py")
        assert info.is_python is True
        assert info.is_test is False
        assert info.is_doc is False
        assert info.is_config is False

    def test_test_file_in_tests_dir(self):
        info = classify_file("tests/test_handler.py")
        assert info.is_python is True
        assert info.is_test is True

    def test_test_file_prefix(self):
        info = classify_file("test_something.py")
        assert info.is_python is True
        assert info.is_test is True

    def test_test_file_suffix(self):
        info = classify_file("something_test.py")
        assert info.is_python is True
        assert info.is_test is True

    def test_conftest(self):
        info = classify_file("tests/conftest.py")
        assert info.is_python is True
        assert info.is_test is True

    def test_doc_markdown(self):
        info = classify_file("docs/guide.md")
        assert info.is_doc is True

    def test_doc_rst(self):
        info = classify_file("docs/changelog.rst")
        assert info.is_doc is True

    def test_readme(self):
        info = classify_file("README.md")
        assert info.is_doc is True

    def test_changelog(self):
        info = classify_file("CHANGELOG.md")
        assert info.is_doc is True

    def test_config_toml(self):
        info = classify_file("pyproject.toml")
        assert info.is_config is True

    def test_config_setup_py(self):
        info = classify_file("setup.py")
        assert info.is_config is True

    def test_config_yaml(self):
        info = classify_file(".github/workflows/ci.yaml")
        assert info.is_config is True

    def test_dockerfile(self):
        info = classify_file("Dockerfile")
        assert info.is_config is True

    def test_non_python_file(self):
        info = classify_file("src/style.css")
        assert info.is_python is False
        assert info.is_test is False
        assert info.is_doc is False


class TestFileChangeInfo:
    def test_is_code_python(self):
        info = FileChangeInfo(filename="src/handler.py", is_python=True)
        assert info.is_code_python is True

    def test_is_code_python_excludes_test(self):
        info = FileChangeInfo(filename="tests/test_handler.py", is_python=True, is_test=True)
        assert info.is_code_python is False

    def test_is_code_python_excludes_doc(self):
        info = FileChangeInfo(filename="README.py", is_python=True, is_doc=True)
        assert info.is_code_python is False

    def test_is_code_python_excludes_config(self):
        info = FileChangeInfo(filename="setup.py", is_python=True, is_config=True)
        assert info.is_code_python is False

    def test_total_changes(self):
        info = FileChangeInfo(filename="file.py", additions=10, deletions=5)
        assert info.total_changes == 15


class TestCheckIssueTextPurity:
    def test_pure_text(self):
        body = "This is a simple issue description with no links or images."
        has_images, has_links, link_count = check_issue_text_purity(body)
        assert has_images is False
        assert has_links is False
        assert link_count == 0

    def test_empty_body(self):
        has_images, has_links, link_count = check_issue_text_purity("")
        assert has_images is False
        assert has_links is False
        assert link_count == 0

    def test_none_body(self):
        has_images, has_links, link_count = check_issue_text_purity(None)
        assert has_images is False
        assert has_links is False
        assert link_count == 0

    def test_markdown_image(self):
        body = "See this screenshot: ![error](https://example.com/img.png)"
        has_images, has_links, _ = check_issue_text_purity(body)
        assert has_images is True

    def test_html_image(self):
        body = 'Here is the bug: <img src="screenshot.png" />'
        has_images, _, _ = check_issue_text_purity(body)
        assert has_images is True

    def test_direct_image_url(self):
        body = "See https://example.com/screenshot.png for details"
        has_images, has_links, _ = check_issue_text_purity(body)
        assert has_images is True
        assert has_links is True

    def test_http_link(self):
        body = "Related to https://github.com/org/repo/issues/123"
        has_images, has_links, link_count = check_issue_text_purity(body)
        assert has_images is False
        assert has_links is True
        assert link_count == 1

    def test_multiple_links(self):
        body = "See https://docs.python.org and also https://github.com/test"
        _, has_links, link_count = check_issue_text_purity(body)
        assert has_links is True
        assert link_count == 2

    def test_code_block_with_no_links(self):
        body = """```python
def foo():
    return "hello"
```

This function needs to handle edge cases better."""
        has_images, has_links, _ = check_issue_text_purity(body)
        assert has_images is False
        assert has_links is False

    def test_code_with_backticks_no_links(self):
        body = "The `some_function()` raises `ValueError` when called with empty input."
        has_images, has_links, _ = check_issue_text_purity(body)
        assert has_images is False
        assert has_links is False


class TestAnalyzeFileChanges:
    def test_basic_analysis(self):
        pr_files = [
            {"filename": "src/core.py", "additions": 20, "deletions": 5, "changes": 25, "status": "modified"},
            {"filename": "tests/test_core.py", "additions": 15, "deletions": 0, "changes": 15, "status": "added"},
            {"filename": "README.md", "additions": 3, "deletions": 1, "changes": 4, "status": "modified"},
        ]
        results = analyze_file_changes(pr_files)
        assert len(results) == 3

        code_files = [r for r in results if r.is_code_python]
        assert len(code_files) == 1
        assert code_files[0].filename == "src/core.py"
        assert code_files[0].additions == 20
        assert code_files[0].deletions == 5

        test_files = [r for r in results if r.is_test]
        assert len(test_files) == 1

        doc_files = [r for r in results if r.is_doc]
        assert len(doc_files) == 1

    def test_empty_file_list(self):
        results = analyze_file_changes([])
        assert results == []


class TestIssueAnalysis:
    def test_to_dict(self):
        analysis = IssueAnalysis(
            owner="test", repo="repo", issue_number=42,
            issue_title="Fix bug", repo_stars=500,
        )
        d = analysis.to_dict()
        assert d["owner"] == "test"
        assert d["repo"] == "repo"
        assert d["issue_number"] == 42
        assert d["issue_title"] == "Fix bug"
        assert d["repo_stars"] == 500
        assert d["meets_criteria"] is False

    def test_issue_body_length(self):
        analysis = IssueAnalysis(owner="a", repo="b", issue_number=1, issue_body="hello")
        assert analysis.issue_body_length == 5

    def test_issue_body_length_empty(self):
        analysis = IssueAnalysis(owner="a", repo="b", issue_number=1)
        assert analysis.issue_body_length == 0
