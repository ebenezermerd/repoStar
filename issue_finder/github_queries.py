from __future__ import annotations

from dataclasses import dataclass

from .github_client import GitHubClient, GitHubApiError
from .types import RepoRef


_ISSUE_CLOSER_QUERY = """
query IssueCloser($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      number
      state
      timelineItems(last: 20, itemTypes: [CLOSED_EVENT]) {
        nodes {
          __typename
          ... on ClosedEvent {
            createdAt
            closer {
              __typename
              ... on PullRequest {
                number
                url
                state
                mergedAt
                closingIssuesReferences(first: 10) {
                  nodes { number }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class ClosingPullRequest:
    number: int
    url: str
    closes_issue_numbers: tuple[int, ...]


def get_single_closing_pr(*, gh: GitHubClient, repo: RepoRef, issue_number: int) -> ClosingPullRequest | None:
    """
    Returns the PR that closed this issue, if and only if:
    - the issue has exactly 1 ClosedEvent with a PullRequest closer
    - that PR's closingIssuesReferences includes exactly 1 issue (itself)
    """
    data = gh.graphql(
        query=_ISSUE_CLOSER_QUERY,
        variables={"owner": repo.owner, "name": repo.name, "number": issue_number},
    )
    try:
        nodes = (
            data["repository"]["issue"]["timelineItems"]["nodes"]
            if data.get("repository") and data["repository"].get("issue")
            else []
        )
    except Exception as e:  # pragma: no cover
        raise GitHubApiError(f"Unexpected GraphQL shape for issue closer: {e}") from e

    closing_prs: list[ClosingPullRequest] = []
    for n in nodes or []:
        if not isinstance(n, dict) or n.get("__typename") != "ClosedEvent":
            continue
        closer = (n.get("closer") or {})
        if closer.get("__typename") != "PullRequest":
            continue
        pr_number = closer.get("number")
        pr_url = closer.get("url")
        closes = tuple(int(x["number"]) for x in ((closer.get("closingIssuesReferences") or {}).get("nodes") or []) if x)
        if isinstance(pr_number, int) and isinstance(pr_url, str):
            closing_prs.append(ClosingPullRequest(number=pr_number, url=pr_url, closes_issue_numbers=closes))

    # Enforce single "closer PR" and one-way closing reference.
    closing_prs = [pr for pr in closing_prs if pr.closes_issue_numbers]
    if len(closing_prs) != 1:
        return None
    pr = closing_prs[0]
    if pr.closes_issue_numbers != (issue_number,):
        return None
    return pr

