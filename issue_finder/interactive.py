"""Interactive CLI for Issue Finder — browse repos, list issues, analyze in real time."""

from __future__ import annotations

import csv
import json

try:
    import readline  # noqa: F401 — enables arrow keys / history in input()
except ImportError:
    pass

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn

from .config import GITHUB_SEARCH_EXCLUSIONS
from .github_client import GitHubClient, RepoInfo, IssueInfo
from .issue_analyzer import IssueAnalyzer, _count_code_python_files, _body_has_links_or_images
from .repo_analyzer import analyze_repo
from .scraper import GitHubScraper
from .history import HistoryStore

console = Console()


# ─── Session ────────────────────────────────────────────────────────────────

class InteractiveSession:
    """Stateful interactive CLI session."""

    def __init__(self, token: str | None = None):
        self.client = GitHubClient(token)
        self.analyzer = IssueAnalyzer(self.client)
        self.scraper = GitHubScraper(token)
        self.history = HistoryStore()

        # State
        self.search_results: list[RepoInfo] = []
        self.selected_repo: RepoInfo | None = None
        self.issues_cache: list[IssueInfo] = []
        self.analysis_results: list[dict] = []
        self.excluded: set[str] = set()

        # Settings
        self.min_stars: int = 200
        self.max_repos: int = 50
        self.max_issues: int = 100
        self.min_score: float = 5.0

    # ── Main loop ───────────────────────────────────────────────

    def run(self) -> int:
        self._print_banner()
        while True:
            try:
                prompt = self._build_prompt()
                line = input(prompt).strip()
                if not line:
                    continue
                self._dispatch(line)
            except KeyboardInterrupt:
                console.print()
                continue
            except EOFError:
                console.print("\n[dim]Goodbye![/dim]")
                return 0

    def _build_prompt(self) -> str:
        base = "\033[1;36missue-finder\033[0m"
        if self.selected_repo:
            repo = f"\033[1;33m{self.selected_repo.full_name}\033[0m"
            return f"{base} [{repo}]> "
        return f"{base}> "

    def _print_banner(self):
        blocked = len(self.history.list_by_status("blocked"))
        worked = len(self.history.list_by_status("worked"))
        extra = ""
        if blocked or worked:
            extra = f"\n[dim]History: {worked} worked, {blocked} blocked[/dim]"
        console.print(Panel(
            "[bold cyan]Issue Finder[/bold cyan] — Interactive Mode\n"
            "[dim]PR Writer HFI Project[/dim]\n\n"
            "Type [bold]help[/bold] for available commands."
            + extra,
            border_style="cyan",
            expand=False,
        ))

    # ── Dispatch ────────────────────────────────────────────────

    def _dispatch(self, raw: str):
        parts = raw.split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        handlers = {
            "search":   self._cmd_search,
            "repos":    self._cmd_repos,
            "repo":     self._cmd_repo,
            "select":   self._cmd_select,
            "info":     self._cmd_info,
            "analyze":  self._cmd_analyze,
            "issues":   self._cmd_issues,
            "issue":    self._cmd_issue,
            "results":  self._cmd_results,
            "export":   self._cmd_export,
            "exclude":  self._cmd_exclude,
            "set":      self._cmd_set,
            "settings": self._cmd_settings,
            "clear":    self._cmd_clear,
            "back":     self._cmd_back,
            "help":     self._cmd_help,
            "quit":     self._cmd_quit,
            "exit":     self._cmd_quit,
            # Smart search presets
            "light":    self._cmd_light,
            "best":     self._cmd_best,
            "label":    self._cmd_label,
            # History commands
            "mark":     self._cmd_mark,
            "history":  self._cmd_history,
            "unblock":  self._cmd_unblock,
        }

        handler = handlers.get(cmd)
        if handler:
            try:
                handler(args)
            except KeyboardInterrupt:
                console.print("\n[dim]Cancelled.[/dim]")
            except Exception as e:
                console.print(f"[red]Error: {escape(str(e))}[/red]")
        else:
            console.print(
                f"[red]Unknown command:[/red] {cmd}. Type [bold]help[/bold] for commands."
            )

    # ── Search commands ─────────────────────────────────────────

    def _cmd_search(self, query: str):
        """Search GitHub for Python repositories (uses web scraping)."""
        if not query:
            console.print("[yellow]Usage:[/yellow] search <keyword>")
            console.print("[dim]  Examples: search pyeve · search sqlfluff · search fastapi[/dim]")
            console.print("[dim]  Presets:  light <keyword> · best <keyword> · label <name>[/dim]")
            return

        console.print(
            f"[cyan]Scraping GitHub search for Python repos matching "
            f"'{escape(query)}' (stars ≥ {self.min_stars})…[/cyan]"
        )
        self.search_results = []

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as prog:
            task = prog.add_task("Scraping search results…", total=None)
            try:
                self.search_results = self.scraper.search_repos(
                    query, language="Python",
                    min_stars=self.min_stars, max_results=self.max_repos,
                )
                prog.update(task, description=f"Found {len(self.search_results)} repos.")
            except Exception:
                prog.update(task, description="Scraping failed, trying API…")
                try:
                    exclude_clause = " ".join(f"NOT {w}" for w in GITHUB_SEARCH_EXCLUSIONS)
                    search_q = f"{query} language:Python stars:>={self.min_stars} {exclude_clause}"
                    for repo in self.client.gh.search_repositories(
                        query=search_q, sort="stars", order="desc",
                    ):
                        if len(self.search_results) >= self.max_repos:
                            break
                        try:
                            self.search_results.append(RepoInfo(
                                full_name=repo.full_name,
                                stars=repo.stargazers_count,
                                size_kb=repo.size,
                                language=repo.language or "",
                                default_branch=repo.default_branch,
                                html_url=repo.html_url,
                                description=repo.description,
                                pushed_at=repo.pushed_at.isoformat() if repo.pushed_at else None,
                            ))
                            prog.update(task, description=f"Found {len(self.search_results)} repos…")
                        except Exception:
                            continue
                except Exception as e2:
                    console.print(f"[red]Search failed: {escape(str(e2))}[/red]")
                    return

        # Filter out blocked repos
        blocked = self.history.blocked_keys()
        if blocked:
            before = len(self.search_results)
            self.search_results = [r for r in self.search_results if r.full_name not in blocked]
            hidden = before - len(self.search_results)
            if hidden:
                console.print(f"[dim]  ({hidden} blocked repos hidden)[/dim]")

        if not self.search_results:
            console.print("[yellow]No repositories found.[/yellow]")
            return

        console.print("[dim]  (scraped via GitHub JSON endpoint)[/dim]")
        self._print_repos_table()

    def _cmd_light(self, query: str):
        """Search for lightweight repos (small, no heavy ML/CUDA deps)."""
        if not query:
            console.print("[yellow]Usage:[/yellow] light <keyword>")
            console.print("[dim]  Finds repos < 50 MB with no heavy deps (pytorch, tensorflow, etc.)[/dim]")
            return

        console.print(f"[cyan]Searching for lightweight Python repos matching '{escape(query)}'…[/cyan]")
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as prog:
            task = prog.add_task("Filtering lightweight repos…", total=None)
            self.search_results = self.scraper.search_light_repos(
                query, min_stars=self.min_stars, max_results=self.max_repos,
            )
            prog.update(task, description=f"Found {len(self.search_results)} light repos.")

        blocked = self.history.blocked_keys()
        self.search_results = [r for r in self.search_results if r.full_name not in blocked]

        if not self.search_results:
            console.print("[yellow]No lightweight repos found.[/yellow]")
            return

        console.print("[dim]  (filtered: < 50 MB, no pytorch/tensorflow/cuda/spark deps)[/dim]")
        self._print_repos_table()

    def _cmd_best(self, query: str):
        """Search for well-maintained, active repos with good issue tracking."""
        if not query:
            console.print("[yellow]Usage:[/yellow] best <keyword>")
            console.print("[dim]  Finds active repos with 500+ stars, recent pushes, good-first-issues[/dim]")
            return

        console.print(f"[cyan]Searching for best Python repos matching '{escape(query)}'…[/cyan]")
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as prog:
            task = prog.add_task("Finding best repos…", total=None)
            self.search_results = self.scraper.search_best_repos(
                query, min_stars=max(self.min_stars, 500), max_results=self.max_repos,
            )
            prog.update(task, description=f"Found {len(self.search_results)} repos.")

        blocked = self.history.blocked_keys()
        self.search_results = [r for r in self.search_results if r.full_name not in blocked]

        if not self.search_results:
            console.print("[yellow]No matching repos found.[/yellow]")
            return

        console.print("[dim]  (filtered: 500+ stars, pushed recently, has good-first-issues)[/dim]")
        self._print_repos_table()

    def _cmd_label(self, args: str):
        """List issues by label for selected repo."""
        if not self._require_repo():
            return
        if not args:
            console.print("[yellow]Usage:[/yellow] label <name>")
            console.print("[dim]  Examples: label bug · label enhancement · label good-first-issue[/dim]")
            return

        console.print(f"[cyan]Scraping issues with label '{escape(args)}' from {self.selected_repo.full_name}…[/cyan]")
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as prog:
            task = prog.add_task("Scraping…", total=None)
            self.issues_cache = self.scraper.search_by_label(
                self.selected_repo.full_name, args, max_issues=self.max_issues,
            )
            prog.update(task, description=f"Found {len(self.issues_cache)} issues.")

        if not self.issues_cache:
            console.print(f"[yellow]No closed issues with label '{args}' found.[/yellow]")
            return

        self._display_issues_table()

    # ── Remaining commands ──────────────────────────────────────

    def _cmd_repos(self, _args: str):
        if not self.search_results:
            console.print("[yellow]No search results. Use [bold]search <keyword>[/bold] first.[/yellow]")
            return
        self._print_repos_table()

    def _cmd_repo(self, name: str):
        if not name:
            console.print("[yellow]Usage:[/yellow] repo <owner/repo>")
            return
        if "/" not in name:
            console.print("[yellow]Format: owner/repo[/yellow]")
            return
        console.print(f"[cyan]Fetching {escape(name)}…[/cyan]")
        info = self.client.get_repo_info(name)
        if not info:
            console.print(f"[red]Repository not found: {name}[/red]")
            return
        self.selected_repo = info
        self.issues_cache = []
        self._print_repo_panel(info)

    def _cmd_select(self, idx_str: str):
        if not self.search_results:
            console.print("[yellow]No search results. Use [bold]search <keyword>[/bold] first.[/yellow]")
            return
        if not idx_str:
            console.print("[yellow]Usage:[/yellow] select <number>")
            return
        try:
            idx = int(idx_str)
        except ValueError:
            console.print("[red]Please provide a number.[/red]")
            return
        if idx < 1 or idx > len(self.search_results):
            console.print(f"[red]Choose 1–{len(self.search_results)}.[/red]")
            return
        self.selected_repo = self.search_results[idx - 1]
        self.issues_cache = []
        self._print_repo_panel(self.selected_repo)

    def _cmd_info(self, _args: str):
        if not self._require_repo():
            return
        self._print_repo_panel(self.selected_repo)

    def _cmd_analyze(self, args: str):
        if not self._require_repo():
            return
        if args:
            try:
                issue_num = int(args.lstrip("#"))
            except ValueError:
                console.print("[red]Provide an issue number, e.g. [bold]analyze 42[/bold][/red]")
                return
            self._analyze_issue(issue_num)
        else:
            self._analyze_repo()

    def _cmd_issues(self, args: str):
        if not self._require_repo():
            return
        limit = self.max_issues
        if args:
            try:
                limit = int(args)
            except ValueError:
                pass
        self._fetch_and_show_issues(limit)

    def _cmd_issue(self, args: str):
        if not self._require_repo():
            return
        if not args:
            console.print("[yellow]Usage:[/yellow] issue <number>")
            return
        try:
            num = int(args.lstrip("#"))
        except ValueError:
            console.print("[red]Provide an issue number.[/red]")
            return
        self._show_issue_detail(num)

    def _cmd_results(self, _args: str):
        if not self.analysis_results:
            console.print("[yellow]No results yet. Use [bold]analyze <issue#>[/bold] to build the list.[/yellow]")
            return
        self._print_results_table()

    def _cmd_export(self, args: str):
        if not self.analysis_results:
            console.print("[yellow]Nothing to export.[/yellow]")
            return
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            console.print("[yellow]Usage:[/yellow] export json <file>  or  export csv <file>")
            return
        self._do_export(parts[0].lower(), parts[1])

    def _cmd_exclude(self, path: str):
        if not path:
            n = len(self.excluded)
            console.print(f"[cyan]Currently excluding {n} issue(s).[/cyan]" if n else "[yellow]Usage:[/yellow] exclude <file>")
            return
        from .main import load_excluded_issues
        self.excluded = load_excluded_issues(path)
        console.print(f"[green]Loaded {len(self.excluded)} excluded issues from {path}[/green]")

    def _cmd_set(self, args: str):
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            console.print("[yellow]Usage:[/yellow] set <key> <value>")
            console.print("[dim]  Keys: min-stars, max-repos, max-issues, min-score[/dim]")
            return
        key = parts[0].lower().replace("_", "-")
        val = parts[1]
        try:
            if key == "min-stars":
                self.min_stars = int(val)
            elif key == "max-repos":
                self.max_repos = int(val)
            elif key == "max-issues":
                self.max_issues = int(val)
            elif key == "min-score":
                self.min_score = float(val)
            else:
                console.print(f"[red]Unknown key: {key}[/red]")
                return
            console.print(f"[green]{key} → {val}[/green]")
        except ValueError:
            console.print(f"[red]Invalid value: {val}[/red]")

    def _cmd_settings(self, _args: str):
        tbl = Table(title="Settings", show_header=True)
        tbl.add_column("Key", style="cyan")
        tbl.add_column("Value", justify="right")
        tbl.add_row("min-stars", str(self.min_stars))
        tbl.add_row("max-repos", str(self.max_repos))
        tbl.add_row("max-issues", str(self.max_issues))
        tbl.add_row("min-score", str(self.min_score))
        tbl.add_row("excluded", str(len(self.excluded)))
        tbl.add_row("token", "[green]✓ set[/green]" if self.client.token else "[red]✗ not set[/red]")
        tbl.add_row("history file", self.history.path)
        tbl.add_row("blocked", str(len(self.history.list_by_status("blocked"))))
        tbl.add_row("worked", str(len(self.history.list_by_status("worked"))))
        console.print(tbl)

    def _cmd_clear(self, _args: str):
        self.analysis_results = []
        self.issues_cache = []
        console.print("[green]Results and issue cache cleared.[/green]")

    def _cmd_back(self, _args: str):
        if self.selected_repo:
            console.print(f"[dim]Deselected {self.selected_repo.full_name}[/dim]")
            self.selected_repo = None
            self.issues_cache = []
        else:
            console.print("[dim]Nothing to go back from.[/dim]")

    # ── History commands ────────────────────────────────────────

    def _cmd_mark(self, args: str):
        """Mark an issue as worked / skipped / blocked."""
        parts = args.split(maxsplit=1)
        if not parts:
            console.print("[yellow]Usage:[/yellow] mark worked | mark skip <reason> | mark block <reason>")
            console.print("[dim]  Marks the last analyzed issue. Or: mark repo block <reason>[/dim]")
            return

        action = parts[0].lower()
        reason = parts[1] if len(parts) > 1 else ""

        # "mark repo block/skip <reason>" — marks the selected repo itself
        if action == "repo":
            if not self._require_repo():
                return
            sub_parts = reason.split(maxsplit=1)
            if not sub_parts:
                console.print("[yellow]Usage:[/yellow] mark repo block <reason>")
                return
            repo_action = sub_parts[0].lower()
            repo_reason = sub_parts[1] if len(sub_parts) > 1 else ""
            status_map = {"block": "blocked", "skip": "skipped", "worked": "worked"}
            status = status_map.get(repo_action)
            if not status:
                console.print("[red]Use: block, skip, or worked[/red]")
                return
            self.history.mark_repo(self.selected_repo.full_name, status, repo_reason)
            console.print(f"[green]Repo {self.selected_repo.full_name} marked as {status}.[/green]")
            return

        if not self.analysis_results:
            console.print("[yellow]No analyzed issues yet. Analyze an issue first.[/yellow]")
            return

        last = self.analysis_results[-1]
        status_map = {"worked": "worked", "work": "worked", "skip": "skipped", "block": "blocked"}
        status = status_map.get(action)
        if not status:
            console.print("[red]Use: worked, skip, or block[/red]")
            return

        self.history.mark(
            repo=last["repo"],
            issue_number=last["issue_number"],
            status=status,
            reason=reason,
            issue_title=last.get("issue_title", ""),
            score=last.get("score", 0),
            pr_number=last.get("pr_number", 0),
            base_sha=last.get("base_sha", ""),
        )
        console.print(
            f"[green]#{last['issue_number']} in {last['repo']} → {status}"
            + (f" ({reason})" if reason else "")
            + "[/green]"
        )

    def _cmd_history(self, args: str):
        """Show tracked history."""
        filt = args.lower() if args else ""
        entries = self.history.all_entries()
        if filt:
            entries = [e for e in entries if e.status == filt]

        if not entries:
            console.print("[yellow]History is empty.[/yellow]" if not filt else f"[yellow]No '{filt}' entries.[/yellow]")
            console.print("[dim]Use [bold]mark worked|skip|block[/bold] after analyzing an issue.[/dim]")
            return

        tbl = Table(title=f"History ({len(entries)})", show_lines=True)
        tbl.add_column("Key", style="cyan", max_width=30)
        tbl.add_column("Status", justify="center")
        tbl.add_column("Title", max_width=35)
        tbl.add_column("Reason", max_width=30)
        tbl.add_column("When", style="dim", max_width=12)

        status_style = {"worked": "[green]worked[/green]", "skipped": "[yellow]skipped[/yellow]", "blocked": "[red]blocked[/red]"}
        for e in entries[:30]:
            tbl.add_row(
                e.key,
                status_style.get(e.status, e.status),
                e.issue_title[:35] if e.issue_title else "",
                e.reason[:30] if e.reason else "",
                e.timestamp[:10] if e.timestamp else "",
            )
        console.print(tbl)
        if len(entries) > 30:
            console.print(f"[dim]  … and {len(entries) - 30} more. Use 'history worked|skipped|blocked' to filter.[/dim]")

    def _cmd_unblock(self, args: str):
        """Remove a blocked entry from history."""
        if not args:
            console.print("[yellow]Usage:[/yellow] unblock <key>")
            console.print("[dim]  Example: unblock owner/repo#123 or unblock owner/repo[/dim]")
            return
        if self.history.get_entry(args):
            self.history.remove(args)
            console.print(f"[green]Removed {args} from history.[/green]")
        else:
            console.print(f"[yellow]{args} not found in history.[/yellow]")

    # ── Help ────────────────────────────────────────────────────

    def _cmd_help(self, _args: str):
        tbl = Table(title="Commands", show_header=True, border_style="cyan", pad_edge=False)
        tbl.add_column("Command", style="bold cyan", min_width=28)
        tbl.add_column("Description")

        rows = [
            ("search <keyword>",      "Search GitHub for Python repos"),
            ("light <keyword>",       "Search lightweight repos (no heavy deps)"),
            ("best <keyword>",        "Search well-maintained, active repos"),
            ("repos",                  "Re-display search results"),
            ("select <n>",            "Select repo # from results"),
            ("repo <owner/repo>",     "Jump directly to a repository"),
            ("info",                   "Show selected repo details"),
            ("analyze",                "Check repo against PR Writer criteria"),
            ("analyze <issue#>",      "Full analysis of a specific issue"),
            ("issues [limit]",        "List closed issues (scraped, no PRs)"),
            ("label <name>",          "List issues by label (e.g. bug, enhancement)"),
            ("issue <number>",        "Inspect a single issue"),
            ("results",                "Show all analyzed issues"),
            ("export json|csv <file>","Export results to file"),
            ("─── History ───",        "─────────────────────────────────────"),
            ("mark worked",           "Mark last analyzed issue as done"),
            ("mark skip <reason>",    "Skip with reason (hidden next time)"),
            ("mark block <reason>",   "Blacklist (auto-hidden in future)"),
            ("mark repo block <why>", "Blacklist entire repo"),
            ("history [status]",      "Show history (filter: worked/skipped/blocked)"),
            ("unblock <key>",         "Remove from blocked list"),
            ("─── Settings ───",       "─────────────────────────────────────"),
            ("set <key> <value>",     "Change setting"),
            ("settings",               "Show current settings"),
            ("exclude <file>",        "Load excluded-issues file"),
            ("clear",                  "Clear cached results"),
            ("back",                   "Deselect current repo"),
            ("help",                   "This message"),
            ("quit / exit",           "Leave interactive mode"),
        ]
        for cmd, desc in rows:
            tbl.add_row(cmd, desc)
        console.print(tbl)

    def _cmd_quit(self, _args: str):
        console.print("[dim]Goodbye![/dim]")
        raise SystemExit(0)

    # ── Helpers ─────────────────────────────────────────────────

    def _require_repo(self) -> bool:
        if self.selected_repo:
            return True
        console.print(
            "[yellow]No repo selected. Use [bold]search[/bold]+[bold]select[/bold] "
            "or [bold]repo <owner/repo>[/bold].[/yellow]"
        )
        return False

    # ── Display helpers ─────────────────────────────────────────

    def _print_repos_table(self):
        tbl = Table(title=f"Search Results ({len(self.search_results)} repos)", show_lines=False)
        tbl.add_column("Row", justify="right", style="bold white", width=4)
        tbl.add_column("Repository", style="cyan")
        tbl.add_column("Stars", justify="right")
        tbl.add_column("Size", justify="right")
        tbl.add_column("Description", max_width=50)

        for i, r in enumerate(self.search_results, 1):
            status = self.history.is_tracked(r.full_name)
            tag = ""
            if status == "worked":
                tag = " [green]✓[/green]"
            elif status == "skipped":
                tag = " [yellow]⊘[/yellow]"
            tbl.add_row(
                str(i),
                r.full_name + tag,
                f"{r.stars:,}",
                f"{r.size_kb / 1024:.1f} MB" if r.size_kb else "—",
                (r.description or "")[:50],
            )
        console.print(tbl)
        console.print("[dim]Use [bold]select <row>[/bold] to pick a repo.[/dim]")

    def _print_repo_panel(self, info: RepoInfo):
        result = analyze_repo(info)
        ok = result.passes
        hist = self.history.is_tracked(info.full_name)
        hist_line = ""
        if hist:
            hist_line = f"\n  History: [bold]{hist}[/bold]"
        console.print(Panel(
            f"[bold]{escape(info.full_name)}[/bold]\n"
            f"  Stars: [bold]{info.stars:,}[/bold]  ·  "
            f"Size: {info.size_kb / 1024:.1f} MB  ·  "
            f"Language: {info.language}\n"
            f"  {info.html_url}\n"
            f"  {escape(info.description or '')}\n\n"
            f"  PR Writer criteria: "
            + ("[green]✓ Passes[/green]" if ok else "[red]✗ Fails[/red]")
            + f"  ({result.summary})"
            + hist_line,
            title=f"Selected: {info.full_name}",
            border_style="green" if ok else "red",
            expand=False,
        ))

    def _analyze_repo(self):
        result = analyze_repo(self.selected_repo)
        tbl = Table(title=f"Repo Analysis: {self.selected_repo.full_name}")
        tbl.add_column("Check", style="bold")
        tbl.add_column("Result")
        for reason in result.reasons:
            ok = "OK" in reason or "Python repo" in reason
            mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
            tbl.add_row(mark, reason)
        tbl.add_row("", "")
        if result.passes:
            tbl.add_row("[bold]Overall[/bold]", f"[bold green]PASSES (score {result.score:.1f})[/bold green]")
        else:
            tbl.add_row("[bold]Overall[/bold]", f"[bold red]FAILS (score {result.score:.1f})[/bold red]")
        console.print(tbl)

    def _fetch_and_show_issues(self, limit: int):
        console.print(
            f"[cyan]Scraping closed issues from {self.selected_repo.full_name} (limit {limit})…[/cyan]"
        )
        self.issues_cache = []
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as prog:
            task = prog.add_task("Scraping issue listing pages…", total=None)
            max_pages = max(1, limit // 25 + 1)
            try:
                self.issues_cache = self.scraper.list_closed_issues(
                    self.selected_repo.full_name, max_pages=max_pages, max_issues=limit,
                )
                prog.update(task, description=f"Scraped {len(self.issues_cache)} issues.")
            except Exception:
                prog.update(task, description="Scraping failed, trying API…")
                for issue in self.client.get_closed_issues(self.selected_repo.full_name, max_issues=limit):
                    self.issues_cache.append(issue)
                    prog.update(task, description=f"Found {len(self.issues_cache)} issues…")

        if not self.issues_cache:
            console.print(
                "[yellow]No closed issues found.[/yellow]\n"
                "[dim]Tip: use [bold]issue <number>[/bold] to look up a specific issue directly.[/dim]"
            )
            return

        # Filter out blocked issues
        blocked = self.history.blocked_keys()
        if blocked:
            before = len(self.issues_cache)
            self.issues_cache = [
                iss for iss in self.issues_cache
                if f"{self.selected_repo.full_name}#{iss.number}" not in blocked
            ]
            hidden = before - len(self.issues_cache)
            if hidden:
                console.print(f"[dim]  ({hidden} blocked issues hidden)[/dim]")

        console.print("[dim]  (scraped from HTML — real issues only, no PRs)[/dim]")
        self._display_issues_table()

    def _display_issues_table(self):
        """Render the issues table with correct issue numbers and labels."""
        tbl = Table(title=f"Closed Issues ({len(self.issues_cache)})", show_lines=False)
        tbl.add_column("Issue #", justify="right", style="green", width=8)
        tbl.add_column("Title", max_width=55)
        tbl.add_column("Labels", style="magenta", max_width=30)
        tbl.add_column("Status", justify="center", width=8)

        repo = self.selected_repo.full_name if self.selected_repo else ""
        for iss in self.issues_cache:
            hist = self.history.is_tracked(repo, iss.number) if repo else ""
            status_icon = ""
            if hist == "worked":
                status_icon = "[green]✓[/green]"
            elif hist == "skipped":
                status_icon = "[yellow]⊘[/yellow]"
            elif hist == "blocked":
                status_icon = "[red]✗[/red]"

            label_str = ", ".join(iss.labels[:3]) if iss.labels else "[dim]—[/dim]"

            tbl.add_row(
                f"#{iss.number}",
                iss.title[:55],
                label_str,
                status_icon,
            )
        console.print(tbl)
        console.print("[dim]Use [bold]analyze <issue#>[/bold] (GitHub issue number, not row).[/dim]")

    def _show_issue_detail(self, num: int):
        issue_info = self._resolve_issue(num)
        if not issue_info:
            return
        body_preview = (issue_info.body or "")[:300]
        if len(issue_info.body or "") > 300:
            body_preview += "…"
        console.print(Panel(
            f"[bold]#{issue_info.number}[/bold]: {escape(issue_info.title)}\n"
            f"  State: {issue_info.state}  ·  Author: {issue_info.user_login}  ·  "
            f"Comments: {issue_info.comments_count}\n"
            f"  {issue_info.html_url}\n"
            f"  Labels: {', '.join(issue_info.labels) or 'none'}\n"
            f"  Created: {issue_info.created_at}  ·  Closed: {issue_info.closed_at or 'open'}\n\n"
            f"[dim]{escape(body_preview)}[/dim]",
            title="Issue Details",
            expand=False,
        ))
        console.print(f"[dim]Use [bold]analyze {num}[/bold] for full PR Writer analysis.[/dim]")

    def _analyze_issue(self, num: int):
        issue_info = self._resolve_issue(num)
        if not issue_info:
            return

        key = f"{self.selected_repo.full_name}#{num}"
        if key in self.excluded:
            console.print(f"[yellow]⚠ Issue {key} is on the excluded list.[/yellow]")
        hist = self.history.is_tracked(self.selected_repo.full_name, num)
        if hist:
            console.print(f"[dim]  History: previously marked as {hist}[/dim]")

        console.print(f"[cyan]Analyzing #{num} (scraper + Timeline API)…[/cyan]")

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as prog:
            task = prog.add_task("Scraping linked PRs via Timeline API…", total=None)
            analysis = self.analyzer.analyze_issue(self.selected_repo.full_name, issue_info)
            prog.update(task, description="Done.")

        body_pure = not _body_has_links_or_images(issue_info.body)
        passes = analysis.passes

        console.print()
        console.print(f"[bold]#{issue_info.number}:[/bold] {issue_info.title}")
        console.print(f"  {issue_info.html_url}")
        console.print(
            f"  Body: {'[green]pure text ✓[/green]' if body_pure else '[red]has links/images ✗[/red]'}"
        )
        score_style = "green" if passes else ("yellow" if analysis.score > 0 else "red")
        console.print(
            f"  Score: [bold {score_style}]{analysis.score:.1f}[/bold {score_style}]  ·  "
            + ("[bold green]PASSES[/bold green]" if passes else "[bold red]DOES NOT PASS[/bold red]")
        )
        if analysis.complexity_hint:
            console.print(f"  Complexity: {analysis.complexity_hint}")

        console.print()
        for reason in analysis.reasons:
            positive = any(
                kw in reason.lower() for kw in ("ok", "pure text", "substantive", "files changed")
            ) and "Only" not in reason and "No " not in reason
            mark = "[green]✓[/green]" if positive else "[red]✗[/red]"
            console.print(f"  {mark} {reason}")

        if analysis.pr_analysis:
            pr = analysis.pr_analysis
            code_files = _count_code_python_files(pr.files)
            console.print()
            console.print(f"  [cyan]Linked PR #{pr.number}[/cyan]  {pr.html_url}")
            console.print(f"    Merged: {pr.merged}  ·  Closes: {pr.closes_issues}")
            console.print(f"    Files: {len(pr.files)} total, {code_files} Python code")
            if pr.base_sha:
                console.print(f"    Base SHA: [bold]{pr.base_sha}[/bold]")
            if pr.files:
                console.print("    Files changed:")
                for f in pr.files:
                    is_test = "test" in f.filename.lower()
                    name = escape(f.filename)
                    if is_test:
                        line = f"      [dim]{name}: +{f.additions}/-{f.deletions}[/dim]"
                    else:
                        line = f"      {name}: [green]+{f.additions}[/green]/[red]-{f.deletions}[/red]"
                    console.print(line)

        base_sha = ""
        if analysis.pr_analysis and analysis.pr_analysis.base_sha:
            base_sha = analysis.pr_analysis.base_sha

        row = {
            "repo": self.selected_repo.full_name,
            "repo_url": self.selected_repo.html_url,
            "stars": self.selected_repo.stars,
            "size_mb": round(self.selected_repo.size_kb / 1024, 2),
            "issue_number": issue_info.number,
            "issue_url": issue_info.html_url,
            "issue_title": issue_info.title,
            "pr_url": analysis.pr_analysis.html_url if analysis.pr_analysis else "",
            "pr_number": analysis.pr_analysis.number if analysis.pr_analysis else 0,
            "score": round(analysis.score, 2),
            "code_files_changed": analysis.details.get("code_python_files_changed", 0),
            "total_additions": analysis.details.get("total_additions", 0),
            "total_deletions": analysis.details.get("total_deletions", 0),
            "complexity_hint": analysis.complexity_hint,
            "reasons": analysis.reasons,
            "base_sha": base_sha,
            "passes": analysis.passes,
        }
        self.analysis_results.append(row)
        console.print(
            f"\n[dim]Result saved ({len(self.analysis_results)} total). "
            f"Use [bold]results[/bold] to review, [bold]mark worked|skip|block[/bold] to track.[/dim]"
        )

    def _print_results_table(self):
        sorted_results = sorted(self.analysis_results, key=lambda r: -r["score"])
        tbl = Table(title=f"Analysis Results ({len(sorted_results)})", show_lines=True)
        tbl.add_column("Row", justify="right", style="bold", width=4)
        tbl.add_column("Repo", style="cyan")
        tbl.add_column("Issue", style="green")
        tbl.add_column("Score", justify="right")
        tbl.add_column("Files", justify="right")
        tbl.add_column("Pass?", justify="center")
        tbl.add_column("Complexity", max_width=22)

        for i, r in enumerate(sorted_results, 1):
            p = r.get("passes", False)
            tbl.add_row(
                str(i),
                r["repo"],
                f"#{r['issue_number']}: {r['issue_title'][:30]}",
                str(r["score"]),
                str(r["code_files_changed"]),
                "[green]✓[/green]" if p else "[red]✗[/red]",
                (r.get("complexity_hint") or "")[:22],
            )
        console.print(tbl)
        console.print("[dim]Use [bold]export json|csv <file>[/bold] to save.[/dim]")

    # ── Issue resolution ────────────────────────────────────────

    def _resolve_issue(self, num: int) -> IssueInfo | None:
        cached = next((i for i in self.issues_cache if i.number == num), None)
        if cached and cached.body is not None:
            return cached

        console.print(f"[dim]Scraping issue #{num}…[/dim]")
        try:
            scraped = self.scraper.get_issue_detail(self.selected_repo.full_name, num)
            if scraped:
                self.issues_cache.append(scraped)
                return scraped
        except Exception:
            pass

        console.print(f"[dim]Falling back to API for #{num}…[/dim]")
        try:
            repo = self.client.get_repo(self.selected_repo.full_name)
            obj = repo.get_issue(num)
            if obj.pull_request:
                console.print(f"[yellow]#{num} is a pull request, not an issue.[/yellow]")
                return None
            info = IssueInfo(
                number=obj.number, title=obj.title, body=obj.body or "",
                state=obj.state, html_url=obj.html_url,
                created_at=obj.created_at.isoformat(),
                closed_at=obj.closed_at.isoformat() if obj.closed_at else None,
                user_login=obj.user.login if obj.user else "",
                comments_count=obj.comments,
                labels=[lb.name for lb in obj.labels],
            )
            self.issues_cache.append(info)
            return info
        except Exception as e:
            console.print(f"[red]Could not fetch issue #{num}: {escape(str(e))}[/red]")
            return None

    # ── Export ──────────────────────────────────────────────────

    def _do_export(self, fmt: str, path: str):
        if fmt == "json":
            with open(path, "w") as f:
                json.dump(self.analysis_results, f, indent=2)
            console.print(f"[green]Saved {len(self.analysis_results)} results → {path}[/green]")
        elif fmt == "csv":
            fieldnames = [
                "repo", "stars", "size_mb", "issue_number", "issue_url",
                "issue_title", "pr_url", "base_sha", "score",
                "code_files_changed", "total_additions", "total_deletions",
                "complexity_hint",
            ]
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(self.analysis_results)
            console.print(f"[green]Saved {len(self.analysis_results)} results → {path}[/green]")
        else:
            console.print("[red]Format must be [bold]json[/bold] or [bold]csv[/bold].[/red]")


# ─── Entry point ────────────────────────────────────────────────────────────

def run_interactive(token: str | None = None) -> int:
    session = InteractiveSession(token)
    return session.run()
