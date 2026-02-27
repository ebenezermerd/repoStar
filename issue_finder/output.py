from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from .types import Candidate


def write_json(path: str | Path, candidates: list[Candidate]) -> None:
    p = Path(path)
    p.write_text(json.dumps([asdict(c) for c in candidates], indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_csv(path: str | Path, candidates: list[Candidate]) -> None:
    p = Path(path)
    rows = [asdict(c) for c in candidates]

    # Stabilize output columns; keep "top_changed_files" readable.
    for r in rows:
        r["top_changed_files"] = " | ".join(r.get("top_changed_files") or [])
        r["reasons"] = " | ".join(r.get("reasons") or [])

    fieldnames = list(rows[0].keys()) if rows else list(asdict(_empty_candidate()).keys())
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _empty_candidate() -> Candidate:
    return Candidate(
        repo_full_name="",
        repo_url="",
        repo_stars=0,
        repo_size_mb=0.0,
        issue_number=0,
        issue_title="",
        issue_url="",
        issue_body_len=0,
        pr_number=0,
        pr_url="",
        base_sha="",
        merge_commit_sha=None,
        changed_py_files=0,
        changed_non_test_doc_py_files=0,
        total_changes_non_test_doc_py=0,
        max_file_changes_non_test_doc_py=0,
        top_changed_files=tuple(),
        score=0.0,
        reasons=tuple(),
    )

