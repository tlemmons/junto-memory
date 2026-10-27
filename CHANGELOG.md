# Changelog

All notable changes to `junto-memory` are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); the server advertises its running
version via `memory_health` (`server_version`).

## [Unreleased]

### Fixed
- **Agents that keep working after parking get idle nudges again.** An agent
  woken while parked usually works on the inbox plugin's session and never calls
  `memory_start_session`, so the parked flag stuck and its idle timer never armed
  (nimbus server-team/billing-team sat at ~700K all night on 2026-10-26). Real
  work while parked (`memory_update_work`, `memory_lock_files`, spec/learning/
  backlog writes, …: `idle_park.UNPARK_TOOLS`) now unparks the agent and arms the
  timer. Reads, acks and `memory_send_message` still don't, so answering one
  message while parked doesn't earn a nudge.

### Changed
- **Auto-reset agents get a direct "park now" nudge.** An agent listed in
  `idle_park_autoreset` is told to park now, shown its reported context size, and
  told the box will /clear + go it after it parks. This overrides the project's
  idle-policy text (e.g. nimbus keep-alive), because the reset only fires on a
  park that follows the nudge. The body still starts with "Idle" for the reset
  gate's anchor (Tom, 2026-10-17).
- **Parked agents are no longer nudged.** After `memory_end_session`, the agent's
  tool calls (including its still-attached inbox plugin's) don't arm an idle timer
  until the agent starts a new session itself; a plugin rebind doesn't count.
  Before this a parked agent was re-armed by its plugin and nudged every ~2h onto
  an expired cache, rebuilding its whole context to reply "still parked"
  (coordinator@nimbus, msg_9684a5d90e59).
- **Idle-park nudge skips agents that already armed a wake.** No nudge while the
  agent has its own pending reminder due within 60 min (e.g. a nimbus keep-alive).
  Before this the 50-min nudge always landed ahead of a +55m keep-alive and reset
  its count, so the chain never timed out and idle agents kept themselves awake for
  hours (learning_222e630bf8b35d24).
- **Idle-park nudge follows the project's idle policy.** The nudge no longer says
  "park now". Its subject is neutral, and a project with an active project-scoped
  guideline `park_on_idle_nudge` gets that guideline's live text in the body
  instead of the default park-at-a-clean-stop advice. Agents with a stale cached
  copy of the guideline still act on the current rule (coordinator@nimbus,
  msg_9f1cf850e7d6).

### Added
- **Local-model delegation** (interface:delegated-inference-v0 1.5.1; README →
  "Local-Model Delegation"). Agents own tasks that run on a local Ollama model:
  `memory_delegate` creates one (connection/scope, instructions, JSON output schema,
  routes, optional schedule and canaries; no schedule = one-off), `memory_schedule`
  manages them (edit is versioned; pause/resume/delete/run_now/history/runs/approve),
  `memory_job` reads one run, `memory_runners` lists runners and connections. The server
  schedules runs and applies routes (`notify` / daily `digest` / `drop`); the model never
  acts. Default model `gpt-oss:20b` (resisted the injection tests). Canaries run daily and
  after any edit to instructions, schema, model or source.
- **Connections + secret vault.** Agents request a connection (imap, mcp, file, inline,
  gmail); the operator activates it in `/admin` by pasting the secret. Secrets are stored
  encrypted under the new **`JUNTO_VAULT_KEY`** env var and sealed per claim to the
  runner's X25519 key. Revoke cancels queued runs; every change is audited.
- **Runner** `contrib/runner/junto-runner.py` + systemd user unit: generic adapters
  (inline, imap read-only, file, mcp source + read-only tools), Ollama structured output,
  LAN-only `/runner/api/*`. `cryptography` is now a pinned direct dependency.
- **Admin web UI** at `/admin` (LAN-only; edits need an owner key): Overview, Settings
  (code default vs. server value, per project), Agents, Alerts (ack), Local tasks, Connections
  (+ audit), Change log.
- **Context history**: gauge readings sampled into `context_history` (10-min buckets,
  90-day TTL); `record_learning` / `change_status` stamp the author's context fill.
- **Action lapse** (design:action-lapse-v0): action messages unanswered for
  `action_lapse_days` (default 30) and answered-but-never-closed tasks/blockers after
  `action_lapse_responded_days` (default 7) leave the action lane (not deleted). Send
  responses add `size_hint` and `possible_parent`; recall rows gain `age_days`.
- `contrib/context-report/junto-context-report.py`: box-agnostic reporter for
  `memory_report_context`, including the idle-park auto-reset gate.
- **Idle-park auto-reset** (design:idle-park-autoreset-v0, phase 2 of the nudge). An agent
  that ends its session while its idle timer is in the nudged state, and is listed in the
  new push-control key `idle_park_autoreset` (comma-separated, opt-in), gets a pending
  `idle_park_resets` doc (one per agent; expires after 6h). `memory_report_context` now
  returns `resets` (pending resets for the agents in that call's reports) and accepts
  `reset_results` ([{reset_id, outcome: done|failed|deferred, detail}]). The per-box
  reporter runs a tmux safe-inject gate (no recent keypress, park banner after the last
  prompt, not busy, no dialog, empty input) and types `/clear` + `go`. Additive: old
  reporters ignore `resets`, so their agents keep nudge-only behaviour.
- **Idle→park nudge** (`src/shared_memory/idle_park.py`). After `idle_park_minutes`
  (default **50**, under the ~60-min prompt-cache TTL) with no real tool call from an
  agent name, the server pushes that agent one `is_system_notice` message
  (`system_notice_kind="idle_park_nudge"`) telling it to park if it's at a clean stop.
  One timer per (project, agent); inbox-plugin background calls (heartbeat, health,
  start_session, emission stats) and `memory_report_context` don't reset it;
  `memory_end_session` disarms it; it fires once per idle period. Only agents whose
  reported context (`memory_report_context` gauge) is at least `idle_park_min_context`
  (default 200K) are nudged; agents with no report are still nudged. New push-control
  config keys `idle_park_minutes` (0 disables), `idle_park_optout` (comma-separated
  agent names) and `idle_park_min_context`, set via `memory_admin push_control_set_config`.

### Fixed
- **Runner 0.4.2:** a gpt-oss reply cut at the cap is retried once at low reasoning effort
  (a phishing canary reasoned past 8,192 tokens); `low_effort_retries` in run evidence.
- **Idle-park nudge no longer fires mid-conversation.** The idle timer counted only junto
  tool calls, so turns spent talking to the user (research, explanation) looked idle and
  coordinator@nimbus was nudged into parking while Tom was working with it. The context
  reporter's `written_at` (stamped by the statusline once per turn) now also resets an
  agent's timer (`idle_park.note_turn`). It never arms a timer, and renders inside the
  nudge's 10-min grace (the agent answering the nudge) don't count. No client change.
- **Runner 0.5.0: parallel runs.** New config `workers` (default 1; sage runs 3): that
  many claim loops share one runner key, and the server's atomic claim keeps them apart.
  Ollama already served 3 requests per model, so the serial runner was the bottleneck
  (measured 1.5x same-model, 2.5x across models on sage's two GPUs). Models bigger than
  `exclusive_model_gb` (default 24, by Ollama's on-disk size, e.g. qwen2.5:72b) run alone,
  so they don't evict the resident models; a waiting exclusive run holds back new runs
  so it can't starve. A server outage no longer kills the runner process; each loop
  logs and retries.
- **Runner 0.4.1:** 0.4.0's 2,048-token reply cap counted gpt-oss reasoning and cut
  replies before any JSON; now min(8,192, context/2), with a clear error on a cap hit.
- **Failed items of scheduled tasks notify the owner** (action notice per run with each
  item's ref, from/subject and error). Before, they were only counted while the cursor
  moved past them.
- **Scheduled tasks' runs are claimed before one-off batches** (`lane` on runs; queued
  runs backfilled at startup).
- **Runner 0.4.0: no more silent truncation or runaway replies.** The runner set neither
  `num_ctx` nor `num_predict`, so Ollama's 4,096-token default silently truncated long
  items, and a model could generate until the 600 s timeout (one call held the queue
  10+ min). It now sizes `num_ctx` per run (buckets 8K/16K, model caps), cuts oversize
  items itself with a visible marker, caps replies at 2,048 tokens, and reports `num_ctx` +
  `items_truncated` in run evidence (only when items exist, so the server's
  "0 items, no evidence" guard still fires).
- Ephemeral reporter identities (`ctx-reporter-*`) replace their earlier sessions on start
  instead of piling up (108 active sessions → 18).
- Spec names that differ only by case are refused; `get_spec` suggests the existing twin.
- Recall no longer serves other agents' session digests and logs scores per recall event.
- Mongo container `nofile` ulimit raised to 64000.
- **Background tasks no longer die when any client disconnects.** The MCP SDK enters
  the app lifespan once per client session; its teardown was cancelling the
  process-global scheduler and SSE keepalive (and resetting the Chroma client) on every
  disconnect. That is why `memory_set_reminder` delivery stalled intermittently. The
  teardown is removed; the idempotent `start_*` calls self-heal on each session.

## [1.39.0] - 2026-09-10

If you run a junto-memory server, the two `Changed` items below alter behavior on
your next restart — read the **Upgrade note** under each.

### Changed
- **Global guidelines now ship in code and seed on every server.**
  `src/shared_memory/global_guidelines.py` is the source of truth for the universal
  `scope="global"` rule set; `seed_global_guidelines()` upserts all of them (by name)
  into `db.guidelines` on every boot. Previously most global rules were DB-resident
  only, so a fresh or peer server started with almost none. Global scope is
  code-managed and **read-only through the `memory_guidelines` tool** (`set`/`delete`
  on a global rule are refused).
  - **Upgrade note:** on your next restart the seeder re-asserts these rules, so a
    live DB edit to a *global* rule is overwritten by the code version. Put fleet- or
    deployment-specific rules under `scope="<project>"` (still writable at runtime via
    `memory_guidelines`). To remove a shipped global, delete it in code **and** delete
    its `db.guidelines` row once (the seeder never deletes non-code rows).
- **`execute-don't-ask` guideline generalized.** The shipped default now states the
  recoverability principle (do reversible work; a human approves the irreversible;
  restoring never needs approval) and **defers the exact approval boundaries to a
  per-fleet "approval contract"** (a spec or project-scoped guideline) instead of
  hard-coding one fleet's staging/prod/merge policy.
  - **Upgrade note:** define your own approval boundaries — e.g.
    `memory_define_spec(name="approval-contract", ...)` or a `scope="<project>"`
    guideline — rather than relying on the shipped rule for your specific gates.
- **Docs reconciled to the "run longer sessions" context bands.** The README, the
  agent/coordinator `CLAUDE.md` templates, and the reference doc now match the
  server's `session_length_discipline` guideline (keep working <500K, watch
  500–800K, park >800K; coordinators shift ~150–200K lower) and drop the stale
  200K-era "park after 1–3 tasks" heuristic.

### Fixed
- **Envelope-leak write-time lint false-negative.** The tail-guard only fired when an
  envelope token sat *immediately* after a field's closing tag, so the two dominant
  real leak shapes slipped through at write time — `record_learning`'s
  `…</details><project>…</invoke>` and `end_session`'s `…</summary><files_modified>…`,
  plus a stray-quote variant. The guard now also recognizes the emitters' own bare
  parameter-tags **when corroborated** by a real envelope token elsewhere in the tail,
  so a doc that merely *documents* the leak shape is never truncated (no false-positive
  data loss). Also routed `end_session` (summary) and `register_function` through the
  shared recovery path. Transparent to callers; broadens write-time leak coverage.

### Added
- **Bearer / header authentication** (`design:header-auth-v0`). When `MCP_AUTH_ENABLED=true`,
  `memory_start_session` now accepts the API key from an `Authorization: Bearer <key>` HTTP
  header in addition to the explicit `api_key` tool argument. This lets adopters configure
  auth the standard MCP way — a `headers` block in `~/.mcp.json` — instead of injecting the
  key into the system prompt. Verified end-to-end against a live Claude Code client
  (CC forwards the static header untouched; the server resolves it to the keyed role).
  - Precedence: explicit `api_key` arg wins; header is the fallback; keyless stays
    `agent`-tier; a present-but-invalid header key hard-rejects (fails loud).
  - New surface: `auth.py` `parse_bearer_token()` + `_header_api_key` contextvar;
    `auth_header_middleware` ASGI wrapper in `__main__.py`; one-line fallback in
    `tools/sessions.py`.

## Earlier

This file was introduced 2026-06-04. For history before this point, see the git log and the
in-server architecture spec (`memory_get_spec(name="architecture:shared-memory-v1")`).
