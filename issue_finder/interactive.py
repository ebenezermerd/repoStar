"""Interactive CLI for Issue Finder — browse repos, list issues, analyze in real time."""

from __future__ import annotations

import asyncio
import csv
import json
import os
import signal
import sys
import termios
import threading
import tty
from pathlib import Path

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
from .issue_analyzer import IssueAnalyzer, _count_code_python_files, _body_has_links_or_images, pre_filter
from .repo_analyzer import analyze_repo
from .scraper import GitHubScraper
from .history import HistoryStore
from .profiles import load_profile, list_profiles, PR_WRITER_PROFILE, ScoringProfile

console = Console()


# ─── ESC key listener ────────────────────────────────────────────────────────

class _EscListener:
    """Context manager that listens for ESC key in a background thread.

    While active, pressing ESC sends SIGINT to the current process,
    which raises KeyboardInterrupt in the main thread — identical to Ctrl+C.
    Terminal is put into raw mode so single key-presses are detected.
    """

    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._old_settings = None

    def __enter__(self):
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return self
        try:
            self._old_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)  # cbreak: single char reads, still allow signals
        except termios.error:
            self._old_settings = None
            return self

        self._stop.clear()
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._old_settings is not None:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_settings)
            except termios.error:
                pass
        self._old_settings = None

    def _listen(self):
        fd = sys.stdin.fileno()
        while not self._stop.is_set():
            if self._stop.wait(0.05):
                break
            try:
                # Check if data available (non-blocking)
                import select as _sel
                rlist, _, _ = _sel.select([fd], [], [], 0.1)
                if rlist:
                    ch = os.read(fd, 1)
                    if ch == b'\x1b':  # ESC
                        os.kill(os.getpid(), signal.SIGINT)
                        break
            except (OSError, ValueError):
                break


def _cancellable(fn):
    """Decorator that wraps a command in the ESC listener.

    While the command runs, pressing ESC or Ctrl+C cancels it.
    """
    def wrapper(self, args):
        with _EscListener():
            return fn(self, args)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


# ─── Session ────────────────────────────────────────────────────────────────

_TOKEN_FILE = Path.home() / ".issue_finder" / "token"


def _load_saved_token() -> str | None:
    """Load persisted GitHub token from disk."""
    try:
        if _TOKEN_FILE.exists():
            t = _TOKEN_FILE.read_text().strip()
            return t if t else None
    except OSError:
        pass
    return None


def _save_token(token: str) -> None:
    """Persist GitHub token to disk."""
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(token + "\n")
    _TOKEN_FILE.chmod(0o600)


def _delete_saved_token() -> bool:
    """Remove persisted token. Returns True if a file was deleted."""
    try:
        if _TOKEN_FILE.exists():
            _TOKEN_FILE.unlink()
            return True
    except OSError:
        pass
    return False


class InteractiveSession:
    """Stateful interactive CLI session."""

    def __init__(self, token: str | None = None):
        # Token priority: CLI arg > env var > saved file
        if token:
            self._token_source = "cli"
        elif os.environ.get("GITHUB_TOKEN"):
            token = os.environ["GITHUB_TOKEN"]
            self._token_source = "env"
        else:
            saved = _load_saved_token()
            if saved:
                token = saved
                self._token_source = "saved"
            else:
                self._token_source = "none"

        self.token = token
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
        self.concurrency: int = 10

        # Profile
        self.profile: ScoringProfile = PR_WRITER_PROFILE

        # Async client (lazy init)
        self._async_client = None
        self._cache = None

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

        # Token status line
        if self.token:
            source_map = {"cli": "via --token", "env": "via GITHUB_TOKEN", "saved": "loaded from disk"}
            src = source_map.get(self._token_source, "")
            token_line = f"\n[green]Token: set ({src})[/green]"
        else:
            token_line = "\n[yellow]Token: not set — use [bold]set token <value>[/bold] to persist one[/yellow]"

        console.print(Panel(
            "[bold cyan]Issue Finder[/bold cyan] — Interactive Mode\n"
            "[dim]PR Writer HFI Project[/dim]\n\n"
            "Type [bold]help[/bold] for available commands.\n"
            "[dim]Press [bold]ESC[/bold] or [bold]Ctrl+C[/bold] to cancel any running operation.[/dim]"
            + token_line + extra,
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
            "unset":    self._cmd_unset,
            "update":   self._cmd_update,
            "settings": self._cmd_settings,
            "clear":    self._cmd_clear,
            "clean":    self._cmd_clean,
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
            # Async / discovery commands
            "discover": self._cmd_discover,
            "profile":  self._cmd_profile,
            "scan":     self._cmd_scan,
            "autoscan": self._cmd_autoscan,
            "cache":    self._cmd_cache,
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

    @_cancellable
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

    @_cancellable
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

    @_cancellable
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

    @_cancellable
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

    @_cancellable
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

    @_cancellable
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

    @_cancellable
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

    @_cancellable
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
            console.print("[dim]  Keys: token, min-stars, max-repos, max-issues, min-score, concurrency[/dim]")
            return
        key = parts[0].lower().replace("_", "-")
        val = parts[1]
        try:
            if key == "token":
                self._apply_token(val)
                _save_token(val)
                self._token_source = "saved"
                console.print("[green]token → ✓ set and saved[/green]")
                console.print(f"[dim]  Stored in {_TOKEN_FILE}[/dim]")
                return
            elif key == "concurrency":
                self.concurrency = int(val)
                self._async_client = None
                console.print(f"[green]concurrency → {val}[/green]")
                return
            elif key == "min-stars":
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

    def _apply_token(self, token: str | None):
        """Apply a token to all clients (session-level)."""
        self.token = token
        self.client = GitHubClient(token)
        self.analyzer = IssueAnalyzer(self.client)
        self.scraper = GitHubScraper(token)
        self._async_client = None  # reset lazy async client

    def _cmd_unset(self, args: str):
        key = args.strip().lower().replace("_", "-")
        if not key:
            console.print("[yellow]Usage:[/yellow] unset token")
            return
        if key == "token":
            self._apply_token(None)
            deleted = _delete_saved_token()
            self._token_source = "none"
            if deleted:
                console.print("[green]Token removed from session and deleted from disk.[/green]")
            else:
                console.print("[green]Token removed from session.[/green] [dim](no saved token on disk)[/dim]")
        else:
            console.print(f"[red]Cannot unset: {key}[/red] [dim](only 'token' is supported)[/dim]")

    def _cmd_update(self, args: str):
        parts = args.split(maxsplit=1)
        if len(parts) < 2:
            console.print("[yellow]Usage:[/yellow] update token <new_value>")
            return
        key = parts[0].lower().replace("_", "-")
        if key == "token":
            val = parts[1]
            self._apply_token(val)
            _save_token(val)
            self._token_source = "saved"
            console.print("[green]Token updated and saved.[/green]")
        else:
            console.print(f"[dim]Tip: use [bold]set {key} <value>[/bold] instead.[/dim]")
            self._cmd_set(args)

    def _cmd_settings(self, _args: str):
        tbl = Table(title="Settings", show_header=True)
        tbl.add_column("Key", style="cyan")
        tbl.add_column("Value", justify="right")
        tbl.add_row("min-stars", str(self.min_stars))
        tbl.add_row("max-repos", str(self.max_repos))
        tbl.add_row("max-issues", str(self.max_issues))
        tbl.add_row("min-score", str(self.min_score))
        tbl.add_row("excluded", str(len(self.excluded)))

        # Token with source info
        if self.token:
            masked = self.token[:4] + "…" + self.token[-4:]
            source_map = {"cli": "via --token", "env": "via GITHUB_TOKEN", "saved": f"from {_TOKEN_FILE}"}
            src = source_map.get(self._token_source, "")
            tbl.add_row("token", f"[green]✓ {masked}[/green] [dim]({src})[/dim]")
        else:
            tbl.add_row("token", "[red]✗ not set[/red]")

        tbl.add_row("profile", f"{self.profile.name} — {self.profile.description}")
        tbl.add_row("concurrency", str(self.concurrency))
        tbl.add_row("history file", self.history.path)
        tbl.add_row("blocked", str(len(self.history.list_by_status("blocked"))))
        tbl.add_row("worked", str(len(self.history.list_by_status("worked"))))
        console.print(tbl)

    def _cmd_clear(self, _args: str):
        self.analysis_results = []
        self.issues_cache = []
        console.print("[green]Results and issue cache cleared.[/green]")

    def _cmd_clean(self, _args: str):
        os.system("clear" if os.name != "nt" else "cls")

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

    # ── Async / Discovery commands ─────────────────────────────

    def _get_async_client(self):
        """Lazy-init async client with cache."""
        if self._async_client is None:
            from .async_client import AsyncGitHubClient
            from .cache import CacheStore
            self._cache = CacheStore(enabled=True)
            self._async_client = AsyncGitHubClient(
                token=self.token,
                cache=self._cache,
                concurrency=self.concurrency,
            )
        return self._async_client

    def _run_async(self, coro):
        """Run an async coroutine from sync context."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    return pool.submit(asyncio.run, coro).result()
            return loop.run_until_complete(coro)
        except RuntimeError:
            return asyncio.run(coro)

    @_cancellable
    def _cmd_discover(self, args: str):
        """Auto-discover repos from trending + topics + curated sources."""
        from .discovery import DiscoveryEngine

        if not self.token:
            console.print(
                "[yellow]Warning: No token set. Discovery uses many API calls and will be slow/rate-limited.[/yellow]\n"
                "[dim]  Set one with: set token <your_github_token>[/dim]"
            )

        sources = ("trending", "topics", "curated")
        if args:
            arg = args.lower().strip()
            valid = {"trending", "topics", "curated"}
            if arg in valid:
                sources = (arg,)
            else:
                console.print(f"[yellow]Unknown source: {arg}. Options: trending, topics, curated[/yellow]")
                return

        client = self._get_async_client()
        engine = DiscoveryEngine(client, self.profile)

        console.print(f"[cyan]Discovering repos from: {', '.join(sources)}…[/cyan]")

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as prog:
            task = prog.add_task("Discovering…", total=None)
            repos = self._run_async(engine.discover(max_repos=self.max_repos, sources=sources))
            prog.update(task, description=f"Found {len(repos)} repos.")

        # Filter out blocked repos
        blocked = self.history.blocked_keys()
        if blocked:
            repos = [r for r in repos if r.full_name not in blocked]

        self.search_results = repos

        if not repos:
            console.print("[yellow]No repos discovered. Try a different source or relax profile criteria.[/yellow]")
            return

        console.print(f"[green]Discovered {len(repos)} repos[/green] [dim](profile: {self.profile.name})[/dim]")
        self._print_repos_table()

    def _cmd_profile(self, args: str):
        """Switch scoring profile or list available profiles."""
        if not args or args.lower() == "list":
            profiles = list_profiles()
            tbl = Table(title="Scoring Profiles", show_header=True)
            tbl.add_column("Name", style="cyan")
            tbl.add_column("Description")
            tbl.add_column("Min Stars", justify="right")
            tbl.add_column("Min Files", justify="right")
            tbl.add_column("Min Score", justify="right")
            tbl.add_column("Active", justify="center")
            for p in profiles:
                active = "[green]✓[/green]" if p.name == self.profile.name else ""
                tbl.add_row(
                    p.name, p.description,
                    str(p.min_stars), str(p.min_code_files_changed),
                    str(p.min_score), active,
                )
            console.print(tbl)
            console.print("[dim]Use [bold]profile <name>[/bold] to switch.[/dim]")
            return

        try:
            self.profile = load_profile(args)
            self.min_stars = self.profile.min_stars
            self.min_score = self.profile.min_score
            self._async_client = None  # reset to pick up new profile
            console.print(f"[green]Switched to profile: {self.profile.name}[/green]")
            console.print(f"[dim]  {self.profile.description}[/dim]")
            console.print(f"[dim]  min_stars={self.profile.min_stars}, min_files={self.profile.min_code_files_changed}, min_score={self.profile.min_score}[/dim]")
        except ValueError as e:
            console.print(f"[red]{e}[/red]")

    @_cancellable
    def _cmd_scan(self, args: str):
        """Scan current repo: list issues → pre-filter → analyze top candidates (async)."""
        if not self._require_repo():
            return

        limit = self.max_issues
        if args:
            try:
                limit = int(args)
            except ValueError:
                pass

        client = self._get_async_client()
        repo = self.selected_repo.full_name

        console.print(f"[cyan]Scanning {repo} (limit {limit}, profile: {self.profile.name})…[/cyan]")

        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as prog:
            task = prog.add_task("Scanning issues…", total=None)
            results = self._run_async(
                client.scan_repo(repo, self.profile, max_issues=limit, pre_filter=pre_filter)
            )
            prog.update(task, description=f"Found {len(results)} passing issues.")

        if not results:
            console.print("[yellow]No passing issues found in this repo.[/yellow]")
            return

        # Add to analysis_results
        for r in results:
            base_sha = ""
            if r.pr_analysis and r.pr_analysis.base_sha:
                base_sha = r.pr_analysis.base_sha
            row = {
                "repo": repo,
                "repo_url": self.selected_repo.html_url,
                "stars": self.selected_repo.stars,
                "size_mb": round(self.selected_repo.size_kb / 1024, 2),
                "issue_number": r.issue.number,
                "issue_url": r.issue.html_url,
                "issue_title": r.issue.title,
                "pr_url": r.pr_analysis.html_url if r.pr_analysis else "",
                "pr_number": r.pr_analysis.number if r.pr_analysis else 0,
                "score": round(r.score, 2),
                "code_files_changed": r.details.get("code_python_files_changed", 0),
                "total_additions": r.details.get("total_additions", 0),
                "total_deletions": r.details.get("total_deletions", 0),
                "complexity_hint": r.complexity_hint,
                "reasons": r.reasons,
                "base_sha": base_sha,
                "passes": r.passes,
            }
            self.analysis_results.append(row)

        console.print(f"[green]Scan complete: {len(results)} issues pass criteria[/green]")
        self._print_results_table()

    @_cancellable
    def _cmd_autoscan(self, _args: str):
        """Full pipeline: discover repos → scan each → show best results."""
        from .discovery import DiscoveryEngine

        if not self.token:
            console.print(
                "[yellow]Warning: No token set. Autoscan makes hundreds of API calls and will be very slow.[/yellow]\n"
                "[dim]  Set one with: set token <your_github_token>[/dim]"
            )

        client = self._get_async_client()
        engine = DiscoveryEngine(client, self.profile)

        console.print(f"[bold cyan]AutoScan[/bold cyan] — full pipeline (profile: {self.profile.name})")
        console.print("[cyan]Step 1/3: Discovering repos…[/cyan]")

        repos = self._run_async(engine.discover(max_repos=self.max_repos))

        blocked = self.history.blocked_keys()
        repos = [r for r in repos if r.full_name not in blocked]

        if not repos:
            console.print("[yellow]No repos discovered.[/yellow]")
            return

        console.print(f"[green]Found {len(repos)} repos[/green]")
        console.print(f"[cyan]Step 2/3: Scanning {len(repos)} repos in parallel…[/cyan]")

        scanned = [0]

        def on_done(repo, results):
            scanned[0] += 1
            hits = f"[green]{len(results)} hits[/green]" if results else "[dim]0[/dim]"
            console.print(f"  [{scanned[0]}/{len(repos)}] {repo.full_name} — {hits}")

        all_results = self._run_async(
            client.scan_repos_parallel(
                repos, self.profile,
                max_issues_per_repo=self.max_issues,
                pre_filter=pre_filter,
                on_repo_done=on_done,
            )
        )

        console.print(f"\n[cyan]Step 3/3: Results[/cyan]")

        if not all_results:
            console.print("[yellow]No passing issues found across all repos.[/yellow]")
            return

        # Add to analysis results
        for r in all_results:
            repo_name = r.issue.html_url.split("/issues/")[0].split("github.com/")[-1]
            base_sha = ""
            if r.pr_analysis and r.pr_analysis.base_sha:
                base_sha = r.pr_analysis.base_sha
            row = {
                "repo": repo_name,
                "repo_url": f"https://github.com/{repo_name}",
                "stars": 0,
                "size_mb": 0,
                "issue_number": r.issue.number,
                "issue_url": r.issue.html_url,
                "issue_title": r.issue.title,
                "pr_url": r.pr_analysis.html_url if r.pr_analysis else "",
                "pr_number": r.pr_analysis.number if r.pr_analysis else 0,
                "score": round(r.score, 2),
                "code_files_changed": r.details.get("code_python_files_changed", 0),
                "total_additions": r.details.get("total_additions", 0),
                "total_deletions": r.details.get("total_deletions", 0),
                "complexity_hint": r.complexity_hint,
                "reasons": r.reasons,
                "base_sha": base_sha,
                "passes": r.passes,
            }
            self.analysis_results.append(row)

        console.print(f"\n[bold green]AutoScan complete: {len(all_results)} issues found across {len(repos)} repos[/bold green]")

        if self._cache:
            stats = self._cache.stats()
            console.print(f"[dim]Cache: {stats['hits']} hits, {stats['misses']} misses ({stats['hit_rate']})[/dim]")

        self._print_results_table()

    def _cmd_cache(self, args: str):
        """Cache management: clear, stats."""
        if not self._cache:
            from .cache import CacheStore
            self._cache = CacheStore(enabled=True)

        action = args.lower().strip() if args else "stats"

        if action == "clear":
            self._cache.invalidate()
            console.print("[green]Cache cleared.[/green]")
        elif action == "stats":
            stats = self._cache.stats()
            tbl = Table(title="Cache Statistics")
            tbl.add_column("Key", style="cyan")
            tbl.add_column("Value", justify="right")
            for k, v in stats.items():
                tbl.add_row(k, str(v))
            console.print(tbl)
        else:
            console.print("[yellow]Usage: cache [clear|stats][/yellow]")

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
            ("─── Discovery (async) ─", "─────────────────────────────────────"),
            ("discover",               "Auto-discover repos (trending+topics+curated)"),
            ("discover trending",     "Only trending Python repos"),
            ("discover topics",       "Only topic-based search"),
            ("discover curated",      "Only curated repo list"),
            ("scan [limit]",          "Scan current repo (async, parallel analysis)"),
            ("autoscan",              "Full pipeline: discover → scan → show best"),
            ("profile [name]",        "Switch scoring profile (or list all)"),
            ("cache [clear|stats]",   "Cache management"),
            ("─── Settings ───",       "─────────────────────────────────────"),
            ("set token <value>",     "Set and persist GitHub token to disk"),
            ("update token <value>",  "Update persisted token (same as set)"),
            ("unset token",           "Remove token from session and disk"),
            ("set <key> <value>",     "Change setting (concurrency, min-stars, etc.)"),
            ("settings",               "Show current settings"),
            ("exclude <file>",        "Load excluded-issues file"),
            ("clear",                  "Clear cached results"),
            ("clean",                  "Clear the terminal screen"),
            ("back",                   "Deselect current repo"),
            ("help",                   "This message"),
            ("quit / exit",           "Leave interactive mode"),
        ]
        for cmd, desc in rows:
            tbl.add_row(cmd, desc)
        console.print(tbl)

    def _cmd_quit(self, _args: str):
        # Clean up async client session
        if self._async_client:
            self._run_async(self._async_client.close())
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

        # Quick-enrich: fetch linked PRs + body in parallel for all issues
        self._quick_enrich_issues()

        console.print("[dim]  (scraped from HTML — real issues only, no PRs)[/dim]")
        self._display_issues_table()

    def _quick_enrich_issues(self):
        """Parallel fetch of linked PRs and body text for listed issues.

        Populates self._issue_metrics with per-issue quick metrics:
        {number: {has_pr, pr_count, body_pure, body_len, pre_filter_pass}}
        """
        self._issue_metrics: dict[int, dict] = {}
        repo = self.selected_repo.full_name

        async def _enrich_all():
            client = self._get_async_client()
            tasks = []
            for iss in self.issues_cache:
                tasks.append(self._enrich_one(client, repo, iss))
            await asyncio.gather(*tasks, return_exceptions=True)

        async def _dummy():
            pass

        try:
            console.print("[dim]  Enriching issues (linked PRs, body check)…[/dim]")
            self._run_async(_enrich_all())
        except (KeyboardInterrupt, Exception):
            pass  # best-effort enrichment

    async def _enrich_one(self, client, repo: str, iss: IssueInfo):
        """Enrich a single issue with quick metrics."""
        metrics: dict = {
            "has_pr": None,
            "pr_count": 0,
            "py_files": None,
            "body_pure": None,
            "body_len": 0,
            "pre_filter": True,
        }

        try:
            # Fetch linked PRs (cached, 1 API call via timeline)
            pr_nums = await client.get_linked_prs(repo, iss.number)
            metrics["pr_count"] = len(pr_nums)
            metrics["has_pr"] = len(pr_nums) > 0

            # Fetch first PR detail to get Python code file count
            if pr_nums:
                pr = await client.get_pr_detail(repo, pr_nums[0])
                if pr and pr.files:
                    metrics["py_files"] = _count_code_python_files(pr.files)

            # Fetch body if missing
            if iss.body is None:
                detail = await client.get_issue_detail(repo, iss.number)
                if detail:
                    iss.body = detail.body
                    iss.user_login = detail.user_login or iss.user_login
                    iss.comments_count = detail.comments_count or iss.comments_count
                    if not iss.labels and detail.labels:
                        iss.labels = detail.labels

            metrics["body_len"] = len(iss.body or "")
            metrics["body_pure"] = not _body_has_links_or_images(iss.body)

            # Quick pre-filter check
            metrics["pre_filter"] = pre_filter(iss, self.profile)
        except Exception:
            pass

        self._issue_metrics[iss.number] = metrics

    def _display_issues_table(self):
        """Render enriched issues table with metrics columns."""
        tbl = Table(
            title=f"Closed Issues — {self.selected_repo.full_name} ({len(self.issues_cache)})",
            show_lines=False,
        )
        min_files = self.profile.min_code_files_changed
        tbl.add_column("#", justify="right", style="green", width=6)
        tbl.add_column("Title", max_width=40)
        tbl.add_column("Labels", style="magenta", max_width=20)
        tbl.add_column("Author", style="dim", max_width=14)
        tbl.add_column("Comments", justify="right", width=8)
        tbl.add_column("PR?", justify="center", width=5)
        tbl.add_column(f"Py({min_files}+)", justify="center", width=7)
        tbl.add_column("Body", justify="center", width=6)
        tbl.add_column("Filter", justify="center", width=6)
        tbl.add_column("Hist", justify="center", width=5)

        repo = self.selected_repo.full_name if self.selected_repo else ""
        metrics = getattr(self, "_issue_metrics", {})

        for iss in self.issues_cache:
            hist = self.history.is_tracked(repo, iss.number) if repo else ""
            hist_icon = ""
            if hist == "worked":
                hist_icon = "[green]✓[/green]"
            elif hist == "skipped":
                hist_icon = "[yellow]⊘[/yellow]"
            elif hist == "blocked":
                hist_icon = "[red]✗[/red]"

            label_str = ", ".join(iss.labels[:2]) if iss.labels else "[dim]—[/dim]"
            author = iss.user_login[:14] if iss.user_login else "[dim]—[/dim]"
            comments = str(iss.comments_count) if iss.comments_count else "[dim]0[/dim]"

            # Metrics from enrichment
            m = metrics.get(iss.number, {})
            if m.get("has_pr") is True:
                pr_icon = f"[green]✓{m['pr_count']}[/green]"
            elif m.get("has_pr") is False:
                pr_icon = "[red]✗[/red]"
            else:
                pr_icon = "[dim]?[/dim]"

            py = m.get("py_files")
            if py is not None:
                py_icon = f"[green]{py}[/green]" if py >= min_files else f"[red]{py}[/red]"
            else:
                py_icon = "[dim]?[/dim]"

            if m.get("body_pure") is True:
                body_icon = "[green]✓[/green]"
            elif m.get("body_pure") is False:
                body_icon = "[red]✗[/red]"
            else:
                body_icon = "[dim]?[/dim]"

            if m.get("pre_filter") is True:
                filter_icon = "[green]✓[/green]"
            elif m.get("pre_filter") is False:
                filter_icon = "[dim]skip[/dim]"
            else:
                filter_icon = "[dim]?[/dim]"

            tbl.add_row(
                f"#{iss.number}",
                iss.title[:40],
                label_str,
                author,
                comments,
                pr_icon,
                py_icon,
                body_icon,
                filter_icon,
                hist_icon,
            )

        console.print(tbl)
        console.print()

        # Legend
        console.print(
            f"[dim]Columns: PR? = has linked PR · Py({min_files}+) = Python code files changed "
            f"([green]green[/green] >= {min_files}) · Body = pure text · Filter = pre-filter[/dim]"
        )
        console.print(
            "[dim]Use [bold]analyze <issue#>[/bold] for full analysis. "
            "Issues with [green]✓[/green] in PR? and Body columns are best candidates.[/dim]"
        )

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
        from .issue_analyzer import (
            IssueAnalysisResult, _is_code_python_file, _is_test_file,
            _is_doc_file, _has_substantial_changes,
        )

        issue_info = self._resolve_issue(num)
        if not issue_info:
            return

        repo = self.selected_repo.full_name
        key = f"{repo}#{num}"
        if key in self.excluded:
            console.print(f"[yellow]⚠ Issue {key} is on the excluded list.[/yellow]")
        hist = self.history.is_tracked(repo, num)
        if hist:
            console.print(f"[dim]  History: previously marked as {hist}[/dim]")

        console.print(f"[cyan]Analyzing #{num} (scraper + Timeline API)…[/cyan]")

        # Use the scraper directly (same path as table enrichment) for consistency
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as prog:
            task = prog.add_task("Finding linked PRs via Timeline API…", total=None)
            pr_nums = self.scraper.get_linked_prs(repo, num)
            prog.update(task, description=f"Found {len(pr_nums)} linked PR(s).")

            best_pr = None
            if pr_nums:
                prog.update(task, description=f"Fetching PR details ({len(pr_nums)} PRs)…")
                # Try to find one-way close PR first
                for pr_num in pr_nums:
                    pr = self.scraper.get_pr_detail(repo, pr_num)
                    if pr and num in pr.closes_issues and len(pr.closes_issues) == 1:
                        best_pr = pr
                        break
                # Fallback: any PR that closes this issue
                if not best_pr:
                    for pr_num in pr_nums:
                        pr = self.scraper.get_pr_detail(repo, pr_num)
                        if pr and num in pr.closes_issues:
                            best_pr = pr
                            break
                # Fallback: first linked PR
                if not best_pr and pr_nums:
                    best_pr = self.scraper.get_pr_detail(repo, pr_nums[0])
            prog.update(task, description="Done.")

        # Build analysis result with scoring
        reasons = []
        details = {}
        score = 0.0

        # Body check
        body_pure = not _body_has_links_or_images(issue_info.body)
        if body_pure:
            score += self.profile.pure_body_score
            reasons.append("Body is pure text")
        else:
            reasons.append("Issue body contains links or images (must be pure text)")

        # PR + files scoring
        code_files = 0
        subst = False
        if best_pr and best_pr.files:
            code_files = _count_code_python_files(best_pr.files)
            subst = _has_substantial_changes(best_pr.files, self.profile.min_substantial_changes)
        details["code_python_files_changed"] = code_files

        if not best_pr:
            reasons.append("No PR found that references this issue")
        elif not (num in best_pr.closes_issues and len(best_pr.closes_issues) == 1):
            reasons.append(f"No PR with one-way close (closes only this issue)")

        if code_files >= self.profile.min_code_files_changed:
            score += self.profile.code_files_score
            reasons.append(f"{code_files} Python code files changed")
        else:
            reasons.append(f"Only {code_files} Python code files changed (need >= {self.profile.min_code_files_changed})")

        if subst:
            score += self.profile.substantial_changes_score
            reasons.append("At least one code file has substantial changes")
        else:
            reasons.append(f"No code file has >= {self.profile.min_substantial_changes} lines changed")

        if len(issue_info.title) >= 10:
            score += self.profile.good_title_score
        else:
            reasons.append("Issue title may be too vague")

        if issue_info.body and len(issue_info.body) > 50:
            score += self.profile.good_description_score
            reasons.append("Issue has substantive description")
        else:
            reasons.append("Issue description may be too brief")

        total_adds = sum(f.additions for f in best_pr.files) if best_pr else 0
        total_dels = sum(f.deletions for f in best_pr.files) if best_pr else 0
        details["total_additions"] = total_adds
        details["total_deletions"] = total_dels
        total_chg = total_adds + total_dels
        if total_chg > 100:
            complexity_hint = "High complexity"
        elif total_chg > 50:
            complexity_hint = "Medium-high complexity"
        elif total_chg > 20:
            complexity_hint = "Medium complexity"
        else:
            complexity_hint = "May be too simple"

        passes = (
            code_files >= self.profile.min_code_files_changed
            and subst
            and best_pr is not None
        )

        analysis = IssueAnalysisResult(
            issue=issue_info,
            pr_analysis=best_pr,
            passes=passes,
            reasons=reasons,
            details=details,
            score=score,
            complexity_hint=complexity_hint,
        )

        body_pure = not _body_has_links_or_images(issue_info.body)
        passes = analysis.passes
        score_style = "green" if passes else ("yellow" if analysis.score > 0 else "red")
        verdict = "[bold green]PASSES[/bold green]" if passes else "[bold red]DOES NOT PASS[/bold red]"

        # ── 1. Issue Header Panel ────────────────────────────────
        issue_body = (issue_info.body or "").strip()

        header_text = (
            f"[bold]#{issue_info.number}[/bold]: {escape(issue_info.title)}\n"
            f"\n"
            f"  URL:      {issue_info.html_url}\n"
            f"  State:    {issue_info.state}  ·  Author: {issue_info.user_login or '—'}\n"
            f"  Labels:   {', '.join(issue_info.labels) or 'none'}\n"
            f"  Comments: {issue_info.comments_count}\n"
            f"  Created:  {issue_info.created_at or '—'}  ·  Closed: {issue_info.closed_at or '—'}"
        )
        console.print(Panel(header_text, title="Issue", border_style="cyan", expand=False))

        # Full issue description in its own panel so links are clearly visible
        if issue_body:
            console.print(Panel(
                escape(issue_body),
                title="Issue Description",
                border_style="cyan" if body_pure else "red",
                subtitle="[red]contains links/images[/red]" if not body_pure else None,
                expand=False,
            ))
        else:
            console.print("[dim]  (no issue description)[/dim]")

        # ── 2. Scoring Breakdown Table ───────────────────────────
        score_tbl = Table(title="Scoring Breakdown", show_header=True, border_style="blue", expand=False)
        score_tbl.add_column("Gate / Criterion", style="bold", min_width=30)
        score_tbl.add_column("Status", justify="center", width=8)
        score_tbl.add_column("Points", justify="right", width=8)
        score_tbl.add_column("Detail", max_width=40)

        # Gate 1: closed
        score_tbl.add_row(
            "Issue is closed",
            "[green]✓[/green]" if issue_info.state == "closed" else "[red]✗[/red]",
            "[dim]gate[/dim]",
            issue_info.state,
        )

        # Gate 2: pure body
        score_tbl.add_row(
            "Body is pure text (no links/images)",
            "[green]✓[/green]" if body_pure else "[red]✗[/red]",
            f"+{self.profile.pure_body_score}" if body_pure else "0",
            f"{len(issue_info.body or '')} chars" + ("" if body_pure else " — contains URLs"),
        )

        # Gate 3: linked PR
        has_pr = analysis.pr_analysis is not None
        score_tbl.add_row(
            "Has linked PR",
            "[green]✓[/green]" if has_pr else "[red]✗[/red]",
            "[dim]gate[/dim]",
            f"PR #{analysis.pr_analysis.number}" if has_pr else "none found",
        )

        # Gate 4: one-way closure
        if has_pr:
            one_way = len(analysis.pr_analysis.closes_issues) == 1
            score_tbl.add_row(
                "PR closes only this issue",
                "[green]✓[/green]" if one_way else "[red]✗[/red]",
                "[dim]gate[/dim]",
                f"closes {analysis.pr_analysis.closes_issues}",
            )

        # Score: code files
        code_files = analysis.details.get("code_python_files_changed", 0)
        min_files = self.profile.min_code_files_changed
        files_pass = code_files >= min_files
        score_tbl.add_row(
            f"Python code files changed (>= {min_files})",
            "[green]✓[/green]" if files_pass else "[red]✗[/red]",
            f"+{self.profile.code_files_score}" if files_pass else "0",
            f"{code_files} files",
        )

        # Score: substantial changes
        score_tbl.add_row(
            f"Substantial changes (>= {self.profile.min_substantial_changes} lines)",
            "[green]✓[/green]" if subst else "[red]✗[/red]",
            f"+{self.profile.substantial_changes_score}" if subst else "0",
            "",
        )

        # Score: title quality
        good_title = len(issue_info.title) >= 10
        score_tbl.add_row(
            "Title >= 10 chars",
            "[green]✓[/green]" if good_title else "[red]✗[/red]",
            f"+{self.profile.good_title_score}" if good_title else "0",
            f"{len(issue_info.title)} chars",
        )

        # Score: body quality
        good_body = issue_info.body and len(issue_info.body) > 50
        score_tbl.add_row(
            "Description > 50 chars",
            "[green]✓[/green]" if good_body else "[red]✗[/red]",
            f"+{self.profile.good_description_score}" if good_body else "0",
            f"{len(issue_info.body or '')} chars",
        )

        # Total row
        max_score = (
            self.profile.pure_body_score + self.profile.code_files_score
            + self.profile.substantial_changes_score + self.profile.good_title_score
            + self.profile.good_description_score
        )
        score_tbl.add_row("", "", "", "")
        score_tbl.add_row(
            f"[bold]Total Score[/bold] (min {self.profile.min_score})",
            verdict,
            f"[bold {score_style}]{analysis.score:.1f}[/bold {score_style}] / {max_score:.1f}",
            analysis.complexity_hint,
        )

        console.print(score_tbl)

        # ── 3. Criteria Checklist ────────────────────────────────
        console.print()
        for reason in analysis.reasons:
            positive = any(
                kw in reason.lower() for kw in ("ok", "pure text", "substantive", "files changed")
            ) and "Only" not in reason and "No " not in reason
            mark = "[green]✓[/green]" if positive else "[red]✗[/red]"
            console.print(f"  {mark} {reason}")

        # ── 4. PR Detail Panel ───────────────────────────────────
        if analysis.pr_analysis:
            pr = analysis.pr_analysis
            code_files_count = _count_code_python_files(pr.files)
            test_files = [f for f in pr.files if _is_test_file(f.filename)]
            doc_files = [f for f in pr.files if _is_doc_file(f.filename)]
            code_file_list = [f for f in pr.files if _is_code_python_file(f.filename)]
            other_files = [f for f in pr.files if f not in code_file_list and f not in test_files and f not in doc_files]

            total_adds = sum(f.additions for f in pr.files)
            total_dels = sum(f.deletions for f in pr.files)
            code_adds = sum(f.additions for f in code_file_list)
            code_dels = sum(f.deletions for f in code_file_list)

            pr_header = (
                f"[bold cyan]PR #{pr.number}[/bold cyan]\n"
                f"\n"
                f"  URL:     {pr.html_url}\n"
                f"  State:   {pr.state}  ·  Merged: {'[green]yes[/green]' if pr.merged else '[red]no[/red]'}\n"
                f"  Closes:  {pr.closes_issues}\n"
                f"  Base SHA: [bold]{pr.base_sha or '—'}[/bold]\n"
                f"\n"
                f"  [bold]Change Summary:[/bold]\n"
                f"    Total files:  {len(pr.files)} ({code_files_count} code, {len(test_files)} test, {len(doc_files)} doc, {len(other_files)} other)\n"
                f"    Total lines:  [green]+{total_adds}[/green] / [red]-{total_dels}[/red] ({total_adds + total_dels} total)\n"
                f"    Code lines:   [green]+{code_adds}[/green] / [red]-{code_dels}[/red]"
            )
            console.print(Panel(pr_header, title="Linked Pull Request", border_style="green" if pr.merged else "yellow", expand=False))

            # PR description
            pr_body = (pr.body or "").strip()
            if pr_body:
                console.print(Panel(
                    escape(pr_body),
                    title="PR Description",
                    border_style="green" if pr.merged else "yellow",
                    expand=False,
                ))
            else:
                console.print("[dim]  (no PR description)[/dim]")

            # Files table
            if pr.files:
                ftbl = Table(title="Files Changed", show_header=True, border_style="dim", expand=False)
                ftbl.add_column("File", max_width=55)
                ftbl.add_column("Type", justify="center", width=6)
                ftbl.add_column("+", justify="right", style="green", width=6)
                ftbl.add_column("-", justify="right", style="red", width=6)
                ftbl.add_column("Total", justify="right", width=6)

                for f in pr.files:
                    if _is_code_python_file(f.filename):
                        ftype = "[bold green]code[/bold green]"
                    elif _is_test_file(f.filename):
                        ftype = "[dim]test[/dim]"
                    elif _is_doc_file(f.filename):
                        ftype = "[dim]doc[/dim]"
                    elif f.filename.endswith(".py"):
                        ftype = "[yellow]py[/yellow]"
                    else:
                        ftype = "[dim]other[/dim]"

                    ftbl.add_row(
                        escape(f.filename),
                        ftype,
                        str(f.additions),
                        str(f.deletions),
                        str(f.additions + f.deletions),
                    )

                console.print(ftbl)

        # ── 5. Save result ───────────────────────────────────────
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
