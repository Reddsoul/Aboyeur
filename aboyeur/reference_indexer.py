"""Reference corpus walker/chunker. Builds/updates nutrition_reference from
plain-text files under REFERENCE_DIR.

Reference files (written by Cookbooks/nutrition_reference_extract.py) look like:

    ---
    title: The Chemistry of Food and Nutrition
    source: gutenberg
    ---

    <full book text, one file per book>

Unlike aboyeur/indexer.py, this is not LLM-extracted structured data — it's
the raw book, chunked at paragraph boundaries for FTS5. No allergy/recipe
semantics apply here; this is prose the agent quotes as historical source
material (see search_nutrition_reference's tool description for the
"not current medical guidance" caveat these books need).
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from aboyeur.config import Config
from aboyeur.db import connect, init_db

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n(.*)$", re.DOTALL)
_TITLE_RE = re.compile(r"^title:\s*(.+)$", re.MULTILINE)

CHUNK_SIZE = 1500  # chars per chunk — small enough for a useful FTS snippet,
CHUNK_OVERLAP = 200  # large enough to keep a paragraph mostly whole


@dataclass
class ReferenceIndexStats:
    scanned: int = 0
    inserted_files: int = 0
    updated_files: int = 0
    unchanged_files: int = 0
    chunks_written: int = 0
    skipped_no_frontmatter: int = 0
    errors: list[str] = field(default_factory=list)


def iter_reference_files(reference_dir: Path) -> Iterator[Path]:
    for p in sorted(reference_dir.glob("*.md")):
        if p.name.startswith(("_", ".")):
            continue
        yield p


def _chunk_text(text: str) -> list[str]:
    """Split text into ~CHUNK_SIZE char chunks at paragraph boundaries,
    overlapping by ~CHUNK_OVERLAP chars so a fact split across a boundary
    still appears whole in one chunk. Same shape as epub_to_recipes.py's
    _chunk_text (Cookbooks repo), applied to full book text instead of one
    section."""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, buf, size = [], [], 0
    for para in paras:
        if size + len(para) > CHUNK_SIZE and buf:
            chunks.append("\n\n".join(buf))
            tail, tail_size = [], 0
            for prev in reversed(buf):
                if tail_size + len(prev) > CHUNK_OVERLAP:
                    break
                tail.insert(0, prev)
                tail_size += len(prev)
            buf, size = tail, tail_size
        buf.append(para)
        size += len(para)
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


def _parse_reference_file(path: Path) -> tuple[str, str] | None:
    """Returns (title, body) or None if no usable frontmatter."""
    text = path.read_text(encoding="utf-8", errors="replace")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    fm_text, body = m.group(1), m.group(2)
    tm = _TITLE_RE.search(fm_text)
    title = tm.group(1).strip() if tm else path.stem
    return title, body.strip()


def _existing_mtime(conn: sqlite3.Connection, path: str) -> float | None:
    row = conn.execute(
        "SELECT mtime FROM nutrition_reference WHERE path = ? LIMIT 1", (path,)
    ).fetchone()
    return row["mtime"] if row else None


def _reindex_file(conn: sqlite3.Connection, path: Path, mtime: float) -> int:
    """Delete any existing chunks for this path and reinsert fresh ones.
    Returns chunk count written."""
    parsed = _parse_reference_file(path)
    if parsed is None:
        return -1
    title, body = parsed
    path_str = str(path)

    conn.execute("DELETE FROM nutrition_reference WHERE path = ?", (path_str,))
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    chunks = _chunk_text(body)
    for i, chunk in enumerate(chunks):
        conn.execute(
            """INSERT INTO nutrition_reference (path, book, chunk_index, text, mtime, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (path_str, title, i, chunk, mtime, now),
        )
    return len(chunks)


def index_reference_corpus(config: Config, rebuild: bool = False, progress_cb=None) -> ReferenceIndexStats:
    """Walk REFERENCE_DIR and (re)chunk each file into nutrition_reference.
    Incremental by file mtime unless rebuild=True — a changed file is fully
    re-chunked (old chunks deleted first), not diffed chunk-by-chunk.
    """
    config.check_reference_dir()
    conn = connect(config.db_path)
    init_db(conn)

    stats = ReferenceIndexStats()
    files = list(iter_reference_files(config.reference_dir))
    total = len(files)

    for i, path in enumerate(files, start=1):
        stats.scanned += 1
        try:
            mtime = path.stat().st_mtime
            existing_mtime = None if rebuild else _existing_mtime(conn, str(path))
            if existing_mtime is not None and existing_mtime == mtime:
                stats.unchanged_files += 1
                if progress_cb and i % 10 == 0:
                    progress_cb(i, total)
                continue

            n_chunks = _reindex_file(conn, path, mtime)
            if n_chunks < 0:
                stats.skipped_no_frontmatter += 1
                continue
            stats.chunks_written += n_chunks
            if existing_mtime is None:
                stats.inserted_files += 1
            else:
                stats.updated_files += 1

            if i % 10 == 0:
                conn.commit()
                if progress_cb:
                    progress_cb(i, total)
        except OSError as e:
            stats.errors.append(f"{path}: {e}")

    conn.commit()
    if progress_cb:
        progress_cb(total, total)
    conn.close()
    return stats
