from setuptools import setup, find_packages

setup(
    name="issue-finder",
    version="1.0.0",
    description="GitHub Issue Finder for PR Writer Project",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "requests>=2.31.0",
        "rich>=13.0.0",
        "click>=8.1.0",
    ],
    entry_points={
        "console_scripts": [
            "issue-finder=issue_finder.cli:main",
        ],
    },
)
