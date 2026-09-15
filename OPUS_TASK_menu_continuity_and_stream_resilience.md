# Task: menu-plan continuity + turn survives client disconnect

Two independent bugs in Aboyeur (personal recipe/nutrition chat app,
FastAPI backend, local LLM via OpenAI-compatible tool-calling). Fix both;
they touch different files and can be separate commits/PRs. Full test
suite (`pytest`) must stay green; add tests for both fixes.

Read these memory files first if available (not required, background only):
`~/.claude/projects/-Volumes-General-Coding-Python-Aboyeur/memory/aboyeur-menu-nutrient-first.md`
and `aboyeur-nameless-toolcall-400.md`.

---

## Bug 1: menu planning can't continue/resume a partial week

**Symptom (user report):** asked for a 7-day meal plan, got a summary
covering only days 3-7. Asked for "the remaining 2 days," got a 2-day plan
with no relation to the first one. Day numbers in the chat don't correspond
to anything real.

**Root cause A — STEP 1 summary is hand-transcribed by the LLM, not
generated in code.** [aboyeur/prompts.py](aboyeur/prompts.py) lines 44-58
require the model to manually write out the day-by-day table (title,
cuisine, macro fit, portion multiplier, totals vs target) from the
`plan_menu` JSON result. This is a known-flaky operation for the local
model — see the existing comment in
[aboyeur/tools/menu.py](aboyeur/tools/menu.py) lines 244-252
(`get_menu_recipes`'s docstring), which documents the *same* class of bug
("day 3's recipes presented under a Day 1 heading") and was fixed for STEP
2 by generating that markdown deterministically in code instead of trusting
the model to transcribe it. STEP 1 never got the equivalent fix.

**Root cause B — `plan_menu` has no continuation/extension mode.**
[aboyeur/tools/menu.py](aboyeur/tools/menu.py) `plan_menu()` (lines
145-241):
- Always numbers days `1..days` internally — see
  [aboyeur/engines/optimizer.py](aboyeur/engines/optimizer.py) line 572:
  `day=slot // meals_per_day + 1`.
- Always inserts a brand-new row via `_save_menu` (menu.py lines 109-117) —
  there is no way to target an *existing* `menu_id` and add days to it.
- `swap_menu_item` (menu.py, later in file) can only replace one
  day/meal slot of an existing menu; it cannot append new days.

So "give me the remaining 2 days" has no real tool to call. The model
can only start a disconnected fresh plan and fabricate day-number framing
in prose — which is exactly what happened.

### Fix required

1. **Make STEP 1's summary table deterministic**, mirroring how
   `get_menu_recipes` already does STEP 2. Add a function (e.g.
   `render_menu_summary(plan: dict) -> str` in `aboyeur/tools/menu.py`,
   returned as part of `plan_menu`'s result, or as a new small helper the
   agent always calls / the tool result already carries pre-formatted
   markdown) that builds the day-by-day table — title, cuisine,
   portion_multiplier when != 1.0, daily_totals vs daily_target_kcal/macros,
   macro_status, gap_notes, overlap/macro-fit score, ingredient_summary —
   directly from the `plan` dict, with correct day numbers guaranteed by
   code, not by model transcription. Update `aboyeur/prompts.py`'s STEP 1
   instructions to say the table itself must be shown/echoed from this new
   deterministic field (same "echo verbatim" contract STEP 2 already uses
   for `get_menu_recipes`'s markdown), not composed freehand from the raw
   JSON. Keep the model's job to add the free-text framing/questions around
   it, not to build the table.

2. **Add a real continuation capability to `plan_menu`.** Design options,
   pick the simplest that fits the existing schema (`days_json` blob in the
   `menus` table, see `_save_menu`/`_load_menu`/`_update_menu` in
   `aboyeur/tools/menu.py`):
   - Add an optional `menu_id` (or `extend_menu_id`) + `start_day` param to
     `plan_menu`. When given an existing menu_id, load its current
     `days_json`, run `optimizer.select_menu` for the *additional* days
     only (reusing the same candidate-pool/target logic), number the new
     picks starting at `existing_max_day + 1`, merge into the existing
     plan's `days` list, recompute the plan-wide fields that depend on the
     full day set (`daily_totals`, `overlap_score`, `macro_fit_avg`,
     `ingredient_summary`, `low_confidence_nutrition`), and `_update_menu`
     the same row instead of creating a new one. Return the merged plan
     under the same `menu_id`.
   - Update `aboyeur/tools/schemas.py`'s `plan_menu` tool schema
     description so the model knows: when the user asks for "the rest of
     the week" / "the remaining N days" / to extend a plan already shown,
     pass the existing `menu_id` and it appends rather than starting over.
   - Update `aboyeur/prompts.py` with an explicit rule covering this case
     (a short new bullet near the existing menu-planning rules), since
     right now there is zero guidance for it and the model is left to
     improvise.
   - Decide on and clearly document behavior when the requested day count
     was originally infeasible/incomplete for reasons other than "user
     asked for fewer days" (e.g. it truly could only fit 5 of 7 requested
     days some other way) — that should surface via the existing
     `cannot_build`/`infeasible_reasons` path, not silent truncation.

3. Add tests: extending a 5-day menu by 2 days yields a 7-day menu under
   the original `menu_id` with days numbered 1-7, no duplicate recipes
   across the merged plan (respect whatever dedup mechanism already exists
   for `select_menu`), and plan-wide totals reflect all 7 days. Also test
   that a fresh (non-extending) `plan_menu` call is unaffected.

---

## Bug 2: refreshing the browser kills an in-progress turn, nothing saved

**Symptom (user report):** a long-running turn (menu planning: many tool
rounds) is abandoned if the page is refreshed/closed mid-stream, instead of
continuing server-side. Nothing about that turn ends up in the transcript.

**Root cause.** [backend/main.py](backend/main.py) `_agent_stream`
(lines 842-890) runs the entire agent turn — every tool round, every
`plan_menu`/`search_recipes` call — as a single Python generator that
*is* the body of a `StreamingResponse` (wired up around line
1079-1087). There is no decoupling from the HTTP connection: turn
lifetime == connection lifetime.

When the client disconnects (tab refresh/close), Starlette throws
`GeneratorExit` into this generator. The only exception handling present
is `except Exception` (main.py line 862), which does **not** catch
`GeneratorExit` (it's a `BaseException`). So the generator dies
immediately, wherever it happened to be — mid `plan_menu` call, mid text
stream, doesn't matter. `_record_message`/`_record_turn_event` (lines
876-890) sit *after* the main loop and are never reached, so nothing is
persisted: no assistant message in the transcript, no `chat_turn` event
logged. Note the in-memory agent's `self.messages` (see
[aboyeur/agent.py](aboyeur/agent.py)'s `Agent._run_turn`,
`_send_turn`'s checkpoint/rollback around lines 263-274) is *also* not
rolled back on `GeneratorExit` for the same reason (except Exception
doesn't catch it) — it's left holding a partial, though structurally
valid, tool-call/tool-result sequence with no final assistant reply
appended. Any DB writes a tool already committed independently (e.g.
`plan_menu`'s `_save_menu` calls `conn.commit()` directly) survive; the
chat transcript and admin telemetry do not.

### Fix required

Decouple agent-turn execution from the HTTP response so a dropped
connection does not abort the turn, and the client can reconnect to see
the result.

Suggested shape (adjust to fit FastAPI/Starlette idioms already used in
this codebase — see `_aboyeur_session_agents` cache at main.py line 615
and `_get_session_agent`/`_reset_session_agent` around lines 672-723 for
the existing per-`(user_id, client_id, chat_session_id)` in-process
caching pattern this can build on):

1. Run the actual agent turn (`_agent_tokens`/`agent.send()`) inside an
   `asyncio` background task (or a small in-process job runner) keyed by
   something like `(chat_session_id, turn_id)`, independent of the request
   that kicked it off. The task owns writing to `chat_history` /
   `_record_turn_event` when it finishes or errors — regardless of whether
   any client is still listening.
2. The task should buffer emitted tokens (in memory is fine given this is
   a single-process homelab app) so that:
   - The original SSE connection streams from that buffer live while
     connected.
   - If the connection drops mid-turn, the background task keeps running
     to completion and still persists the final transcript/event row.
   - A reconnect (new GET/SSE request for the same `chat_session_id`, or a
     small polling endpoint) can pick up from the buffer — either replay
     what was buffered so far and keep streaming, or (simplest acceptable
     version) just poll "is a turn in flight for this session, and is it
     done yet" so the frontend can show a spinner and then fetch the
     saved transcript once done, instead of silently losing the turn.
   - Wrap the task's `except Exception` handling around the *whole* turn
     the same way `_agent_stream` already does today (log + persist a
     partial/failure notice), so error behavior doesn't regress.
3. Make sure only one in-flight turn per `chat_session_id` at a time (guard
   against a second message arriving while a background turn is still
   running for the same session) — reuse or extend the existing session
   dict as the guard.
4. Keep `agent.py`'s existing checkpoint/rollback behavior
   (`Agent._send_turn`, lines 263-274) meaningful: since the turn now runs
   to completion regardless of client connection, a genuine mid-turn
   exception should still roll `self.messages` back so the cached agent
   stays usable for the next turn — this should now actually get exercised
   as `except Exception`, not bypassed by `GeneratorExit`, since the
   background task is not itself an HTTP-connection-bound generator.
5. Frontend: minimal change to handle "turn still running, catching you
   up" on reconnect — check `frontend`/whatever serves the chat UI (find
   the fetch/EventSource call site) and adjust so a refresh mid-turn shows
   the in-progress/completed state instead of just looking broken. If a
   full reconnect-and-replay UI is too large for this task, at minimum:
   on load, check whether the most recent turn for the active session is
   still in-flight and show a "generating..." state that polls until the
   transcript is updated, rather than presenting stale state as if nothing
   happened.

### Tests to add
- A turn whose generator consumer is abandoned partway (simulate client
  disconnect) still results in a persisted assistant message +
  `chat_turn` event once the background task finishes.
- No double-persistence when both the original connection and a
  reconnect end up observing the same completed turn.
- Only one in-flight turn per session is allowed; a second message sent
  while one is running is rejected or queued (pick one, document it) —
  not silently interleaved into the same `Agent.messages` from two call
  sites at once.

---

## Constraints / house rules to respect while doing this

- Every route currently opens its own short-lived DB connection rather
  than sharing one across threads/tasks (see the comment above
  `_profile_conn` in main.py) — keep that pattern for whatever writes the
  background task does.
- `MAX_TOOL_ROUNDS = 60` in `aboyeur/agent.py` is an intentional generous
  ceiling (see its comment) — don't touch it as part of either fix.
- Don't reintroduce the fixed nameless-tool-call / malformed-tool-args
  bugs (`aboyeur/llm.py` `StreamCollector`, `aboyeur/agent.py`
  `_parse_tool_args`) — leave that machinery alone.
- Keep `_sse()`'s multi-line-safe framing (main.py lines 818-825) as the
  wire format if you keep SSE for the live-streaming half of Bug 2's fix.
- This is a single-process homelab deployment (no Redis/Celery in the
  stack) — an in-process asyncio task + in-memory buffer is an acceptable
  and preferred solution; don't introduce a new infra dependency for this.

## Deliverable

Working code + passing `pytest` (existing suite + new tests above) for
both bugs. Note in the PR/commit description which design option you
picked for Bug 1's continuation mechanism and Bug 2's reconnect UX, and
why, if it differs from the suggested shape above.
