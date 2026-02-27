"""Rich terminal output formatting for issue analysis results."""

import json
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.columns import Columns
from rich import box

from .filters import IssueAnalysis

console = Console()


def complexity_color(score: float) -> str:
    if score >= 50:
        return "green"
    if score >= 30:
        return "yellow"
    return "red"


def display_issue_detail(analysis: IssueAnalysis):
    """Display detailed analysis of a single issue."""
    status_color = "green" if analysis.meets_criteria else "red"
    status_text = "MEETS CRITERIA" if analysis.meets_criteria else "REJECTED"

    title = Text()
    title.append(f"Issue #{analysis.issue_number}: ", style="bold")
    title.append(analysis.issue_title, style="bold cyan")
    title.append(f"  [{status_text}]", style=f"bold {status_color}")

    console.print()
    console.print(Panel(title, box=box.DOUBLE))

    info_table = Table(show_header=False, box=box.SIMPLE, padding=(0, 2))
    info_table.add_column("Key", style="bold", width=24)
    info_table.add_column("Value")

    info_table.add_row("Repository", f"{analysis.owner}/{analysis.repo}")
    info_table.add_row("Issue URL", analysis.issue_url)
    info_table.add_row("Stars", f"{analysis.repo_stars:,}")
    info_table.add_row("Repo Size", f"{analysis.repo_size_mb:.1f} MB")
    info_table.add_row("", "")

    purity = "Yes" if analysis.is_pure_text else "No"
    purity_style = "green" if analysis.is_pure_text else "red"
    info_table.add_row("Pure Text Description", Text(purity, style=purity_style))
    info_table.add_row("Has Images", "Yes" if analysis.has_images else "No")
    info_table.add_row("Has Links", f"{'Yes' if analysis.has_links else 'No'} ({analysis.link_count} found)")
    info_table.add_row("Description Length", f"{analysis.issue_body_length} chars")
    info_table.add_row("", "")

    if analysis.pr_number:
        info_table.add_row("Linked PR", f"#{analysis.pr_number}")
        info_table.add_row("PR URL", analysis.pr_url)
        info_table.add_row("PR Merged", "Yes" if analysis.pr_merged else "No")
        info_table.add_row("Base SHA", analysis.base_sha[:12] if analysis.base_sha else "N/A")
        info_table.add_row("", "")
        info_table.add_row("Python Code Files Changed", str(analysis.total_python_code_files))
        info_table.add_row("Test Files Changed", str(analysis.total_test_files_changed))
        info_table.add_row("Doc Files Changed", str(analysis.total_doc_files_changed))
        info_table.add_row("Total Additions", f"+{analysis.total_additions}")
        info_table.add_row("Total Deletions", f"-{analysis.total_deletions}")
        info_table.add_row("Largest Change File", f"{analysis.max_change_file} ({analysis.max_file_changes} lines)")
    else:
        info_table.add_row("Linked PR", "None found")

    info_table.add_row("", "")
    color = complexity_color(analysis.complexity_score)
    info_table.add_row("Complexity Score", Text(f"{analysis.complexity_score:.1f}", style=f"bold {color}"))

    console.print(info_table)

    if analysis.file_changes:
        console.print()
        console.print(Text("File Changes:", style="bold underline"))
        file_table = Table(box=box.ROUNDED, show_lines=False)
        file_table.add_column("File", style="cyan", max_width=60)
        file_table.add_column("Type", width=10)
        file_table.add_column("+", style="green", justify="right", width=6)
        file_table.add_column("-", style="red", justify="right", width=6)
        file_table.add_column("Total", justify="right", width=7)

        for fc in sorted(analysis.file_changes, key=lambda f: -f.total_changes):
            ftype = []
            if fc.is_python:
                ftype.append("py")
            if fc.is_test:
                ftype.append("test")
            if fc.is_doc:
                ftype.append("doc")
            if fc.is_config:
                ftype.append("cfg")
            if not ftype:
                ftype.append("other")

            file_table.add_row(
                fc.filename,
                ", ".join(ftype),
                f"+{fc.additions}",
                f"-{fc.deletions}",
                str(fc.total_changes),
            )
        console.print(file_table)

    if analysis.rejection_reasons:
        console.print()
        console.print(Text("Rejection Reasons:", style="bold red"))
        for reason in analysis.rejection_reasons:
            console.print(f"  • {reason}", style="red")

    if analysis.issue_body:
        console.print()
        body_preview = analysis.issue_body[:500]
        if len(analysis.issue_body) > 500:
            body_preview += "..."
        console.print(Panel(body_preview, title="Issue Description Preview", box=box.ROUNDED))


def display_results_table(results: list[IssueAnalysis], title: str = "Issue Analysis Results"):
    """Display a summary table of all analyzed issues."""
    table = Table(title=title, box=box.ROUNDED, show_lines=True)
    table.add_column("#", width=4, justify="right")
    table.add_column("Repository", style="cyan", max_width=25)
    table.add_column("Issue", max_width=40)
    table.add_column("PR", width=6, justify="right")
    table.add_column("Stars", justify="right", width=7)
    table.add_column("Size MB", justify="right", width=8)
    table.add_column("Py Files", justify="right", width=8)
    table.add_column("+/-", justify="right", width=10)
    table.add_column("Pure", width=5)
    table.add_column("Score", justify="right", width=7)
    table.add_column("Status", width=8)

    for i, a in enumerate(results, 1):
        status = Text("PASS", style="bold green") if a.meets_criteria else Text("FAIL", style="bold red")
        pure = Text("Yes", style="green") if a.is_pure_text else Text("No", style="red")
        color = complexity_color(a.complexity_score)

        table.add_row(
            str(i),
            f"{a.owner}/{a.repo}",
            f"#{a.issue_number}: {a.issue_title[:35]}",
            str(a.pr_number or "-"),
            f"{a.repo_stars:,}",
            f"{a.repo_size_mb:.1f}",
            str(a.total_python_code_files),
            f"+{a.total_additions}/-{a.total_deletions}",
            pure,
            Text(f"{a.complexity_score:.0f}", style=color),
            status,
        )

    console.print()
    console.print(table)

    passing = sum(1 for a in results if a.meets_criteria)
    console.print(
        f"\n  Total: {len(results)} issues analyzed, "
        f"[green]{passing} passing[/green], "
        f"[red]{len(results) - passing} rejected[/red]"
    )


def export_results_json(results: list[IssueAnalysis], filepath: str):
    """Export results to a JSON file."""
    data = [a.to_dict() for a in results]
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)
    console.print(f"\nResults exported to [cyan]{filepath}[/cyan]")


def export_results_csv(results: list[IssueAnalysis], filepath: str):
    """Export results to a CSV file."""
    import csv
    if not results:
        console.print("[yellow]No results to export[/yellow]")
        return

    fieldnames = list(results[0].to_dict().keys())
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for a in results:
            row = a.to_dict()
            row["rejection_reasons"] = "; ".join(row["rejection_reasons"])
            writer.writerow(row)
    console.print(f"\nResults exported to [cyan]{filepath}[/cyan]")
