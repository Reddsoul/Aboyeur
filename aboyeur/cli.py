"""CLI entrypoints: aboyeur index, aboyeur stats, aboyeur search, aboyeur chat."""

from __future__ import annotations

import argparse
import sys
import time

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

from aboyeur.config import Config
from aboyeur.db import connect

console = Console()


def cmd_index(args: argparse.Namespace) -> int:
    from aboyeur.indexer import index_corpus

    config = Config.load()
    console.print(f"[bold]RECIPES_DIR[/bold] {config.recipes_dir}")
    console.print(f"[bold]DB_PATH[/bold]      {config.db_path}")
    console.print(f"[bold]mode[/bold]         {'rebuild (full reparse)' if args.rebuild else 'incremental'}")

    start = time.monotonic()
    try:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("indexing", total=None)

            def on_progress(done: int, total: int) -> None:
                progress.update(task, total=total, completed=done)

            stats = index_corpus(config, rebuild=args.rebuild, progress_cb=on_progress)
    except FileNotFoundError as e:
        console.print(f"[bold red]error:[/bold red] {e}")
        return 1

    elapsed = time.monotonic() - start
    console.print()
    console.print(f"scanned:   {stats.scanned}")
    console.print(f"inserted:  {stats.inserted}")
    console.print(f"updated:   {stats.updated}")
    console.print(f"unchanged: {stats.unchanged}")
    console.print(f"skipped (no frontmatter): {stats.skipped_no_frontmatter}")
    console.print(f"skipped (malformed):      {stats.skipped_malformed}")
    if stats.errors:
        console.print(f"[yellow]{len(stats.errors)} errors (first 10):[/yellow]")
        for e in stats.errors[:10]:
            console.print(f"  {e}")
    console.print(f"took {elapsed:.1f}s")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    config = Config.load()
    if not config.db_path.exists():
        console.print(f"[bold red]error:[/bold red] no DB at {config.db_path} — run `aboyeur index` first")
        return 1

    conn = connect(config.db_path)
    n_recipes = conn.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    n_books = conn.execute("SELECT COUNT(DISTINCT book) FROM recipes").fetchone()[0]
    n_cuisines = conn.execute(
        "SELECT COUNT(DISTINCT cuisine) FROM recipes WHERE cuisine IS NOT NULL"
    ).fetchone()[0]
    n_embedded = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

    console.print(f"[bold]recipes[/bold]  {n_recipes}")
    console.print(f"[bold]books[/bold]    {n_books}")
    console.print(f"[bold]cuisines[/bold] {n_cuisines}")
    console.print(f"[bold]embedded[/bold] {n_embedded} (vector search not wired yet)")

    console.print()
    console.print("[bold]top 10 books by recipe count[/bold]")
    for row in conn.execute(
        "SELECT book, COUNT(*) c FROM recipes GROUP BY book ORDER BY c DESC LIMIT 10"
    ):
        console.print(f"  {row['c']:>6}  {row['book']}")

    console.print()
    console.print("[bold]top 10 cuisines[/bold]")
    for row in conn.execute(
        "SELECT cuisine, COUNT(*) c FROM recipes WHERE cuisine IS NOT NULL "
        "GROUP BY cuisine ORDER BY c DESC LIMIT 10"
    ):
        console.print(f"  {row['c']:>6}  {row['cuisine']}")

    conn.close()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Quick FTS5 sanity-check from the CLI (not the LLM tool — see aboyeur/tools/)."""
    config = Config.load()
    if not config.db_path.exists():
        console.print(f"[bold red]error:[/bold red] no DB at {config.db_path} — run `aboyeur index` first")
        return 1

    conn = connect(config.db_path)
    start = time.monotonic()
    rows = conn.execute(
        """SELECT r.title, r.book, r.cuisine, bm25(recipes_fts) AS rank
           FROM recipes_fts
           JOIN recipes r ON r.id = recipes_fts.rowid
           WHERE recipes_fts MATCH ?
           ORDER BY rank
           LIMIT ?""",
        (args.query, args.limit),
    ).fetchall()
    elapsed_ms = (time.monotonic() - start) * 1000

    for row in rows:
        console.print(f"  [bold]{row['title']}[/bold]  ({row['book']}, {row['cuisine']})")
    console.print(f"\n{len(rows)} results in {elapsed_ms:.1f}ms")
    conn.close()
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    config = Config.load()
    if not config.db_path.exists():
        console.print(f"[bold red]error:[/bold red] no DB at {config.db_path} — run `aboyeur index` first")
        return 1

    from aboyeur.agent import Agent

    conn = connect(config.db_path)
    agent = Agent(conn, config)

    console.print("[bold]Aboyeur[/bold] — your 60k-recipe cookbook, expedited. 'exit' to leave.\n")
    try:
        while True:
            try:
                user_input = console.input("[bold cyan]you>[/bold cyan] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                break

            console.print("[bold green]aboyeur>[/bold green] ", end="")
            for kind, payload in agent.send(user_input, debug=args.debug):
                if kind == "text":
                    console.print(payload, end="", markup=False, highlight=False)
                elif kind == "tool_call":
                    console.print(f"\n  [dim]→ {payload['name']}[/dim]", end="")
                    console.print(f"({payload['args']})", markup=False, highlight=False)
                elif kind == "tool_result":
                    preview = str(payload["result"])
                    if len(preview) > 300:
                        preview = preview[:300] + "…"
                    console.print(f"  [dim]← {preview}[/dim]", markup=False, highlight=False)
            console.print("\n")
    finally:
        conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aboyeur")
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="build/update the recipe index from RECIPES_DIR")
    p_index.add_argument("--rebuild", action="store_true", help="full reparse, ignore mtimes")
    p_index.set_defaults(func=cmd_index)

    p_stats = sub.add_parser("stats", help="print corpus/index stats")
    p_stats.set_defaults(func=cmd_stats)

    p_search = sub.add_parser("search", help="quick FTS5 query (debug only)")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.set_defaults(func=cmd_search)

    p_chat = sub.add_parser("chat", help="conversational REPL")
    p_chat.add_argument("--debug", action="store_true", help="show tool-call trace")
    p_chat.set_defaults(func=cmd_chat)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
