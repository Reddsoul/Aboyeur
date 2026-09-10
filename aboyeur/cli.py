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


def cmd_index_reference(args: argparse.Namespace) -> int:
    from aboyeur.reference_indexer import index_reference_corpus

    config = Config.load()
    console.print(f"[bold]REFERENCE_DIR[/bold] {config.reference_dir}")
    console.print(f"[bold]DB_PATH[/bold]       {config.db_path}")
    console.print(f"[bold]mode[/bold]          {'rebuild (full rechunk)' if args.rebuild else 'incremental'}")

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

            stats = index_reference_corpus(config, rebuild=args.rebuild, progress_cb=on_progress)
    except FileNotFoundError as e:
        console.print(f"[bold red]error:[/bold red] {e}")
        return 1

    elapsed = time.monotonic() - start
    console.print()
    console.print(f"scanned:        {stats.scanned}")
    console.print(f"inserted files: {stats.inserted_files}")
    console.print(f"updated files:  {stats.updated_files}")
    console.print(f"unchanged:      {stats.unchanged_files}")
    console.print(f"chunks written: {stats.chunks_written}")
    console.print(f"skipped (no frontmatter): {stats.skipped_no_frontmatter}")
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
    n_ref_chunks = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='nutrition_reference'"
    ).fetchone()[0]
    n_ref_chunks = (
        conn.execute("SELECT COUNT(*) FROM nutrition_reference").fetchone()[0] if n_ref_chunks else 0
    )
    n_ref_books = (
        conn.execute("SELECT COUNT(DISTINCT book) FROM nutrition_reference").fetchone()[0]
        if n_ref_chunks else 0
    )

    console.print(f"[bold]recipes[/bold]  {n_recipes}")
    console.print(f"[bold]books[/bold]    {n_books}")
    console.print(f"[bold]cuisines[/bold] {n_cuisines}")
    console.print(f"[bold]embedded[/bold] {n_embedded} (vector search not wired yet)")
    console.print(f"[bold]reference chunks[/bold] {n_ref_chunks} ({n_ref_books} books) — run `aboyeur index-reference` if 0")

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


def _resolve_cli_client(conn, user_id: int, name: str | None) -> dict:
    """Pick the client this CLI session plans for: --client by name (exact,
    case-insensitive), else the account's default."""
    from aboyeur import clients as clients_mod

    if name:
        for client in clients_mod.list_clients(conn, user_id, include_archived=True):
            if client["name"].strip().lower() == name.strip().lower():
                return client
        known = ", ".join(c["name"] for c in clients_mod.list_clients(conn, user_id)) or "none yet"
        raise SystemExit(f"no client named {name!r} for this account. Known clients: {known}")
    return clients_mod.get_client(conn, user_id, clients_mod.default_client_id(conn, user_id))


def cmd_chat(args: argparse.Namespace) -> int:
    config = Config.load()
    if not config.db_path.exists():
        console.print(f"[bold red]error:[/bold red] no DB at {config.db_path} — run `aboyeur index` first")
        return 1

    from aboyeur import auth
    from aboyeur.agent import Agent
    from aboyeur.db import init_db, schema_lock
    from aboyeur.migrate import SEED_USER_EMAIL, migrate

    conn = connect(config.db_path)
    with schema_lock(config.db_path):
        init_db(conn)
        migrate(conn)  # idempotent; no-op once already migrated
    # The CLI is a single local user, not the multi-tenant web path — resolve
    # (or first-run create) an account by email rather than requiring a
    # password login. --user overrides the default household owner.
    user_id = auth.get_or_create_user(conn, args.user or SEED_USER_EMAIL)
    # The CLI has no client picker, so it plans for the account's default
    # client (its oldest active one, created on first use). --client picks a
    # different one by name.
    client = _resolve_cli_client(conn, user_id, getattr(args, "client", None))
    agent = Agent(conn, config, client["id"], client_name=client["name"])
    console.print(f"[dim]planning for:[/dim] [bold]{client['name']}[/bold]")

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


def cmd_create_user(args: argparse.Namespace) -> int:
    """Provision an investor/tester account — invite-only, no public signup
    route. Prints the plain password once; nothing else displays or stores it."""
    import getpass

    from aboyeur import auth
    from aboyeur.db import init_db

    config = Config.load()
    conn = connect(config.db_path)
    init_db(conn)

    password = args.password or getpass.getpass(f"password for {args.email}: ")
    if not password:
        console.print("[bold red]error:[/bold red] password cannot be empty")
        return 1
    try:
        user_id = auth.create_user(conn, args.email, password)
    except auth.AuthError as e:
        console.print(f"[bold red]error:[/bold red] {e}")
        return 1
    finally:
        conn.close()

    if getattr(args, "admin", False):
        from aboyeur import admin as admin_mod

        conn = connect(config.db_path)
        try:
            admin_mod.set_admin(conn, args.email, True)
        finally:
            conn.close()

    console.print(
        f"[bold green]created[/bold green] user {user_id} ({args.email})"
        + (" [bold]— admin[/bold]" if getattr(args, "admin", False) else "")
    )
    return 0


def cmd_set_admin(args: argparse.Namespace) -> int:
    """Grant or revoke the /admin console. Out-of-band on purpose — no route
    in the app can promote an account, so a compromised session can't either."""
    from aboyeur import admin as admin_mod
    from aboyeur.db import init_db, schema_lock
    from aboyeur.migrate import migrate

    config = Config.load()
    conn = connect(config.db_path)
    try:
        # A deploy that hasn't run the migration yet has no is_admin column;
        # granting is exactly when someone is most likely to hit that.
        with schema_lock(config.db_path):
            init_db(conn)
            migrate(conn)
        user_id = admin_mod.set_admin(conn, args.email, not args.revoke)
    except admin_mod.AdminError as e:
        console.print(f"[bold red]error:[/bold red] {e}")
        return 1
    finally:
        conn.close()

    verb = "revoked admin from" if args.revoke else "granted admin to"
    console.print(f"[bold green]{verb}[/bold green] user {user_id} ({args.email.strip().lower()})")
    return 0


def cmd_purge_events(args: argparse.Namespace) -> int:
    """Event-log retention. Point cron at this — the table is one row per
    agent turn, so it grows forever otherwise (slowly, but forever)."""
    from aboyeur import admin as admin_mod

    config = Config.load()
    conn = connect(config.db_path)
    try:
        deleted = admin_mod.purge_events(conn, args.keep_days)
    finally:
        conn.close()
    console.print(f"[bold green]purged[/bold green] {deleted} event(s) older than {args.keep_days}d")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aboyeur")
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="build/update the recipe index from RECIPES_DIR")
    p_index.add_argument("--rebuild", action="store_true", help="full reparse, ignore mtimes")
    p_index.set_defaults(func=cmd_index)

    p_index_ref = sub.add_parser("index-reference", help="build/update the nutrition-reference index from REFERENCE_DIR")
    p_index_ref.add_argument("--rebuild", action="store_true", help="full rechunk, ignore mtimes")
    p_index_ref.set_defaults(func=cmd_index_reference)

    p_stats = sub.add_parser("stats", help="print corpus/index stats")
    p_stats.set_defaults(func=cmd_stats)

    p_search = sub.add_parser("search", help="quick FTS5 query (debug only)")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.set_defaults(func=cmd_search)

    p_chat = sub.add_parser("chat", help="conversational REPL")
    p_chat.add_argument("--debug", action="store_true", help="show tool-call trace")
    p_chat.add_argument("--user", help="practitioner account's email (default: the seed account)")
    p_chat.add_argument("--client", help="which client to plan for by name (default: the account's first client)")
    p_chat.set_defaults(func=cmd_chat)

    p_create_user = sub.add_parser("create-user", help="provision an account (invite-only, admin use)")
    p_create_user.add_argument("email")
    p_create_user.add_argument("--password", help="omit to be prompted (hidden input)")
    p_create_user.add_argument("--admin", action="store_true",
                               help="also grant the /admin console")
    p_create_user.set_defaults(func=cmd_create_user)

    p_purge = sub.add_parser("purge-events", help="drop admin event rows older than N days")
    p_purge.add_argument("--keep-days", type=int, default=90)
    p_purge.set_defaults(func=cmd_purge_events)

    p_set_admin = sub.add_parser("set-admin", help="grant/revoke the /admin console")
    p_set_admin.add_argument("email")
    p_set_admin.add_argument("--revoke", action="store_true", help="take it away instead")
    p_set_admin.set_defaults(func=cmd_set_admin)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
