"""In-process agent-turn runner: a turn outlives the HTTP connection that
started it.

Before this, /chat ran the whole agent turn (every tool round, every
plan_menu call) as the body of a StreamingResponse — turn lifetime ==
connection lifetime. A tab refresh mid-stream threw GeneratorExit into the
generator, which `except Exception` doesn't catch, so the turn died wherever
it was and nothing (no assistant message, no chat_turn event) was persisted.

Now a Turn owns the work: a daemon thread runs the agent and appends every
emitted chunk to an in-memory buffer; any number of readers (the original
SSE response, a reconnect after refresh) stream from that buffer, and a
reader going away changes nothing for the worker. Persistence is the
worker's job, done exactly once in its own finally — readers never write.

Single-process homelab app: threads + a dict are the whole job runner. The
agent loop is synchronous (blocking LLM/USDA calls), so a thread, not an
asyncio task, is the right container for it.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Callable, Hashable, Iterator, Optional

logger = logging.getLogger("aboyeur")

# Finished turns stay in the registry this long so a reconnect that lands
# just after completion can still see "done" (and replay the text) instead
# of "nothing here". After that the transcript is the only record needed.
FINISHED_TTL_S = 15 * 60


class TurnInFlight(Exception):
    """A turn is already running for this key; a second one is rejected, not
    queued — two turns can't interleave into one Agent's message history."""


class Turn:
    def __init__(self, key: Optional[Hashable] = None):
        self.key = key
        self.id = uuid.uuid4().hex
        self.chunks: list[str] = []
        self.done = False
        self.error = False
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self._cond = threading.Condition()
        self._thread: Optional[threading.Thread] = None

    # -- worker side ---------------------------------------------------------

    def start(self, fn: Callable[["Turn"], None]) -> None:
        """Run fn(self) on a daemon thread. fn appends chunks via
        self.append(); finish() is guaranteed afterwards even if fn raises,
        so readers can never block forever on a dead worker."""
        def _run() -> None:
            try:
                fn(self)
            except BaseException:  # last line of defence — fn is expected to catch its own
                logger.exception("turn %s worker died", self.id)
                self.finish(error=True)
            else:
                self.finish()

        self._thread = threading.Thread(target=_run, name=f"turn-{self.id[:8]}", daemon=True)
        self._thread.start()

    def append(self, chunk: str) -> None:
        with self._cond:
            self.chunks.append(chunk)
            self._cond.notify_all()

    def finish(self, error: bool = False) -> None:
        with self._cond:
            if self.done:
                return
            self.done = True
            self.error = self.error or error
            self.finished_at = time.time()
            self._cond.notify_all()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until the turn is done. Returns whether it is."""
        with self._cond:
            self._cond.wait_for(lambda: self.done, timeout=timeout)
            return self.done

    # -- reader side ---------------------------------------------------------

    def stream(self, start: int = 0) -> Iterator[str]:
        """Yield chunks from index `start`, then keep yielding live ones until
        the turn is done. Safe for any number of concurrent readers; a reader
        that is closed part-way (client disconnect -> GeneratorExit) leaves
        the worker untouched."""
        i = max(0, start)
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self.done or len(self.chunks) > i)
                batch = self.chunks[i:]
                finished = self.done
            i += len(batch)
            for c in batch:
                yield c
            if finished and i >= len(self.chunks):
                return

    def text(self) -> str:
        with self._cond:
            return "".join(self.chunks)

    def snapshot(self) -> dict:
        with self._cond:
            return {
                "turn_id": self.id,
                "in_flight": not self.done,
                "done": self.done,
                "error": self.error,
                "chunk_count": len(self.chunks),
                "text": "".join(self.chunks),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


class TurnRegistry:
    """One Turn per key (chat_session_id) — the in-flight guard and the thing
    a reconnect looks up."""

    def __init__(self, finished_ttl_s: float = FINISHED_TTL_S):
        self._turns: dict[Hashable, Turn] = {}
        self._lock = threading.Lock()
        self._ttl = finished_ttl_s

    def _evict_stale(self) -> None:
        now = time.time()
        for key, t in list(self._turns.items()):
            if t.done and t.finished_at is not None and now - t.finished_at > self._ttl:
                del self._turns[key]

    def begin(self, key: Hashable) -> Turn:
        """Reserve `key` with a new, not-yet-started Turn. Raises TurnInFlight
        if one is still running there. Reserve BEFORE writing the user
        message to the transcript so a rejected duplicate leaves no orphan."""
        with self._lock:
            self._evict_stale()
            current = self._turns.get(key)
            if current is not None and not current.done:
                raise TurnInFlight(key)
            turn = Turn(key)
            self._turns[key] = turn
            return turn

    def release(self, key: Hashable, turn: Turn) -> None:
        """Undo begin() for a turn that never started (e.g. the route failed
        between reserving and launching)."""
        with self._lock:
            if self._turns.get(key) is turn:
                del self._turns[key]

    def get(self, key: Hashable) -> Optional[Turn]:
        with self._lock:
            self._evict_stale()
            return self._turns.get(key)

    def in_flight(self, key: Hashable) -> bool:
        t = self.get(key)
        return t is not None and not t.done
