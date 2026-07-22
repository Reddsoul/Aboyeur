# Kickoff prompt for Claude Code

Paste this to start the build (or start a fresh session in this directory and say "read task.md and start Phase 1"):

---

Read `task.md` in full — it is the complete build spec for Aboyeur and the source of truth. Follow it exactly; where it is silent, prefer simple over clever.

Context you need before writing code:

1. **The corpus is real — look at it.** Read 3–5 recipe files from different books under `RECIPES_DIR` (see task.md §11) to see the actual frontmatter and body format before writing the parser. Handle real-world mess: missing `cuisine`, missing SERVES, unicode fractions (½, ¼), files without frontmatter (skip them).
2. **`poc.py` and `backend/` are reference material, not a starting point.** Port the logic named in task.md §2 (macro math, USDA cache, staples exclusion, shopping categorization); do not import from them or extend them.
3. **The recipes dir is an SMB mount and can be slow or stale.** The indexer must be incremental, show progress, and fail with a clear message if the mount is missing. Never assume the mount in `chat` — only `index` touches it, plus verbatim recipe reads (handle failure gracefully).

Build discipline:

- Work phase by phase (task.md §9). At the end of each phase, run the acceptance checks and show me the actual output (real commands, real results) before moving on.
- Write tests as you go, not after. Copy ~20 real recipe files into `tests/fixtures/` so tests never touch the mount.
- The LLM endpoint may be unreachable while you build — Phases 1–2 must be fully testable without it. For Phase 3, if the endpoint is down, stub the LLM behind the same interface and tell me.
- Allergy filtering is safety-critical: it lives in the tool layer, it is tested, and no code path hands the LLM a recipe containing a profile allergen.
- Keep files under ~400 lines. Type hints everywhere. No premature abstraction — no plugin systems, no base classes with one subclass.

Start with Phase 1. Before writing code, print a short plan of the files you'll create and the schema DDL, then build it.

---
