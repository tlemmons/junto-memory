# Push Control — design writeup

**Status:** Filed as `design:push-control-v0` v1.1.0 by memory@junto on 2026-05-19. Working doc; the registered spec at v1.1.0 mirrors this content.

**Supersedes:** `design:autopilot-delivery-filter-v0` (the held-queue surface tool, the agent-visible marker, and the release-trigger as a named concept; the delivery-time filter mechanism it required is preserved — see §11).

**Numbers locked in this version (validated against junto + nimbus message archive, 2026-05-12 → 2026-05-19, n=383):** depth cap **12**, push budget **30 / sender / hour**, hard ceiling **100 / sender / hour**.

---

## 1. Problem and threat model

Agents in Junto message each other to do work. Most of that traffic is supposed to flow
without a human in the loop — that is the entire point of the channel system. But an
agent that malfunctions can send messages in a loop: re-sending stale directives,
re-acking, re-deriving a goal it already completed. Left unattended, that loop burns
model-turn usage and context budget on a conversation no one will ever read.

The control system exists to catch that. It does **not** exist to defend against a
malicious agent.

This distinction is the core of the proposal, and it is where the previous design drifted:

- **Security model** — defends against an agent that *deliberately* tries to bypass the
  brake. Requires forgery resistance, server-derived everything, sender-claims-untrusted.
- **Safety model** — catches an agent that is *confused*. A confused agent does not exploit
  the brake; it does not even know the brake exists. It just keeps sending.

Junto today is a closed, single-operator environment: every agent is an instance the
operator configured, running on the operator's hardware, behind Tailscale, with credentials
the operator issued. In that setting the realistic failure is a *confused* agent, not a
*malicious* one.

But this design is intended for wider use — public release, and deployment into other
environments such as a work setting — where the operator does not configure every agent and
the agents may run on models from more than one vendor. So the safety/security distinction
must be drawn on the mechanism, not on an assumption about who built the agents:

- The control layers below are **mechanical and model-agnostic**. They count; they do not
  judge. They contain a *confused* agent — a runaway loop — regardless of which model or
  vendor it runs on, and regardless of whether the operator is the one who configured it.
  That protection travels intact to any deployment.
- What this system is **not** is a hardened defense against an agent that *deliberately*
  games the brake. That is a real scope boundary (see §14), deliberate, not an oversight.
  For a closed single-operator environment it is the correct boundary. An adopter inherits
  it: the confused-agent protection travels, but anyone placing genuinely untrusted agents
  on the message bus needs controls beyond this design.

**Therefore: this is a safety system, not a security system.** It assumes agents that may
*malfunction*, not agents that are *adversarial*.

The realistic triggers, in rough order of likelihood, are plumbing bugs (a channel plugin
re-delivering a seen message, a stale session-id causing re-introduction), prompt or state
drift (a guideline that structurally produces an ack-loop), misconfiguration (two agents on
one identity, a test harness left running), and — least common — a genuinely confused model.

It is a **low-frequency, non-zero, high-cost-when-unattended** event. The justification for
building it is not frequency; it is asymmetry. The event is rare, the guard is nearly free,
and an unguarded incident running overnight is genuinely expensive. Rare × cheap-guard ×
expensive-miss = build it, keep it simple, then mostly forget about it.

The existing destructive-content gate (the keyword tripwire that forces human review) is a
**security** mechanism and is correct as-is. It stays, unchanged, orthogonal to everything
below. Nothing in this document touches it.

---

## 2. Core principle: push control is invisible to agents

Push control is invisible to agents by design. They are given no role in it. Agents have
exactly one experience of this system: a message is pushed into their context, or it is not.
They carry no rules about caps, no markers in their prompts, no count in their context, no
awareness that a brake exists. The gating logic lives entirely server-side and is invisible
to the agent on both ends — sender and receiver.

This is a deliberate boundary, not a limitation. The agents are not lacking anything; the
mechanism is simply not theirs to participate in. Keeping it server-side is what makes it
robust — an agent cannot mishandle, ignore, or drift away from a brake it has no contact
with.

This is stronger and simpler than `autopilot-delivery-filter-v0`, which still had a
held-queue tool, a statusline badge, and a release-trigger mechanism the agent interacted
with. Here, none of that exists. The agent cannot distinguish "no message exists" from
"a message exists but was not pushed." That is the correct boundary: the brake is a property
of the *system*, not a thing any agent participates in.

There is no "mode." There is no "autopilot." Every message, the server makes one decision:
push, or don't.

**The one acknowledged exception** is the post-incident recovery notice (§8). After a spiral
is contained, the server inserts a single plain-language message into the affected agents'
inboxes describing what happened. This is a controlled point of contact — but it does not
breach the principle above. The agent still carries *no rule* about push control; it simply
receives one message that happens to describe a past push-control event, and reasons about
it as ordinary inbox content. It conveys a historical fact, post-incident, for the agent to
evaluate — not a standing rule to follow on every message. That is categorically different
from the `autopilot-delivery-filter-v0` marker, whose cost was a permanent interpretive rule
in every agent's prompt. One after-the-fact factual message is not that.

---

## 3. The model: send vs push

Two distinct actions, kept separate on purpose:

- **Send** — a sender creates a message. It is persisted, always, regardless of any limit.
  Sending always succeeds from the sender's point of view. The sender never receives an
  error, never sees a limit, never has anything to retry or work around.
- **Push** — the server puts a message into the receiver's context and wakes it. This is the
  controlled action. The server decides, per message, whether to push.

A message that is sent but not pushed is **not lost**. It persists in the receiver's normal
inbox and is picked up the next time that agent's inbox is surfaced — for an interactive
agent, the next human-driven session (a `go`, a `status`, any user turn). It is delivered
silently — present but not announced. This is the fail-loud principle the team already
established: suppress, never drop. The data is always there; only the wake is withheld.

(The "picked up on the next human-driven session" assumption holds for interactive agents,
which is the only mode Junto uses today. Non-interactive agents are a known unsolved case —
see §9.)

**How the server distinguishes "human-driven session" from "background poll" for
plugin-mediated agents.** The junto-inbox channel plugin polls inbox state continuously
(every 10–25s) and pumps results to its host CC as channel notifications. Without a gating
mechanism, un-pushed messages would just flow through that path and the suppress-push
primitive would be moot. The server therefore filters the plugin-poll response to exclude
push-suppressed messages by default, and releases them only when the recipient agent has had
a **recent human turn** — defined by the same recency-bypass logic already used elsewhere
(5-minute window, triggered by any of: `memory_start_session` for that agent, an inbound
message with `sent_by_human=True` to that agent, or an outbound from that agent with
`human_interacted=True`). When the recency window is open, push-suppressed messages become
eligible for the next plugin-poll response and surface as channel blocks. When it closes,
suppression re-engages.

The plugin and the agent are unaware of this — they continue to call the same poll path; the
server's response shape changes silently based on recency state. No new tool is introduced,
no agent-visible release-trigger, no API addition; the mechanism repurposes signals the
server already observes for chain-depth recency bypass.

---

## 4. The three control layers

All counts are partitioned **by project** as a namespace — Junto's counts never mix with
Nimbus's. Within a project:

| Layer | Counts | Scope | At the limit | Agent sees |
|---|---|---|---|---|
| Push depth cap | Replies in one chain — a flat count, no resets | Per **thread** | Stop pushing that chain | Nothing |
| Push budget | Auto-messages a sender has emitted this window | Per **sender**, hourly | Stop pushing that sender's messages | Nothing |
| Hard ceiling | Same sender count, higher threshold | Per **sender**, hourly | Suspend pushing for that agent + alert Tom | Nothing (it simply goes quiet) |
| Destructive gate | Keyword tripwire on content | Per message | Force human review | n/a — separate security mechanism |

The first three are the safety system. They are effectively **one sender counter read at
two thresholds, plus one per-thread counter** — not three separate accounting systems. Cheap
to implement, cheap to run, invisible on the happy path.

Starting numbers (see §13 for how these are configured — server-level defaults, overridable
per project, human-modifiable):

- Push depth cap: **~12**
- Push budget: **30 / sender / hour**
- Hard ceiling: **100 / sender / hour**

The depth cap is a **flat ceiling on replies in a single chain, with no resets** (see §6 for
why there is no human-engagement reset). It must be set above the length of any healthy
exchange. A normal work conversation between two agents bounces a handful of times, resolves,
and ends on its own — it seldom approaches 12. A chain that reaches 12 unresolved replies
should be checked, since hitting that limit is unusual. Nimbus runs an hourly budget of 30
and it works well; the depth number is set higher than Nimbus's original because, as a hard
flat cap with no reset, it must clear any genuine multi-step exchange — including a
contract negotiation (proposal → critique → revise → critique → accept), which is the
longest healthy chain shape and still well under 12. The cap's job is not to keep a human in
the loop — it is to catch the chain that has *stopped being productive*.

---

## 5. What happens at each limit

**Depth cap and push budget — silent containment.** When either soft limit is hit, the
server stops pushing. The message is still accepted, still persisted, still sits in the
receiver's inbox. The sender gets a normal success response. Nobody gets an error.

This is deliberate. A confused agent that received an error would not stop — it does not
know it is confused. It would treat the error as transient and retry, or "helpfully" work
around it, tightening the loop and burning context on error-handling. A confused agent
cannot spiral against a brake it cannot perceive. So the brake is silent: no error, no
marker, no signal, nothing to react to.

The honest cost: a confused agent getting silent success **will keep sending**. Its messages
just stop getting pushed and accumulate, un-announced, in inboxes. That is acceptable for
the soft layers — the data is persisted, nothing is lost, the spiral cannot spread (no push
= no wake = no cascade), and the sender's count is climbing on Tom's readout where it is
visible. The system *contains*; the human *resolves*.

**The dashboard counter is a v0 deliverable on claudeControl's side.** The soft-containment
story specifically requires the human to *see* the sender's count climbing — without that
surface, the human has no signal that a soft-contained spiral is in progress. claudeControl
exposes a per-agent emission counter (current-hour count, with the budget and ceiling
thresholds rendered as reference lines). Counter data lives on memory's side; claudeControl
reads it via a memory tool on the same poll cadence as inbox views. Exact tool surface
(likely a `memory_get_emission_stats` or equivalent) is pinned during implementation; the
spec-level commitment is that the surface exists in v0.

**Hard ceiling — the backstop.** Silent containment protects the *system*, but it does not
protect *usage and context budget* — the confused agent keeps consuming model turns all
night even though nothing it sends gets pushed. The hard ceiling is what bounds that.

Without it, the overnight exposure is "agent loop-speed × hours until Tom wakes" — a few
hundred to a few thousand model turns, unbounded in any way Tom controls. With it, the
exposure is bounded at the ceiling, then the agent is neutralized, regardless of whether Tom
looks in one hour or twelve. The hard ceiling is **not optional** and belongs in v0; it is
the layer that actually delivers the protection the system was built for.

---

## 6. Counting scopes, and why — and why there is no human reset

**Depth cap → per thread, flat, no reset.** A runaway loop *is* a chain that will not
terminate. Counting replies within one `in_response_to` lineage maps exactly onto that
failure mode. When two agents are stuck bouncing, that one chain's count climbs at full rate
and trips; every other conversation in the project is untouched. Tightest possible isolation.

The depth counter has **no reset.** Earlier drafts of this design tried to reset it when a
human was "engaged" with the chain. That idea was abandoned, and the reasoning is worth
recording because it is easy to reach for again:

- *Resetting on a user-tier message* fails because Tom rarely sends messages directly. He
  drives an agent from a terminal and tells it to send. That outbound message is authored by
  an agent-tier credential — the server sees nothing human about it.
- *Resetting on a "human-attended session"* fails because, in Junto, **every** session is
  human-attended in the structural sense — Tom starts each agent in its own terminal. But he
  then walks away. A session stays "attended" while no human is within reach of it for hours.
  Attendance is not a state the server can observe; it is a fading thing.
- *Resetting on a `human_interacted=true` flag* — the flag a sender can set to assert "this
  send is on the trailing edge of a human prompt" — is a third proxy, and in the safety model
  it is *not* a weak signal: a confused sender has no reason to lie about it, and it has
  worked reliably for the existing recency-window bypass. It is nonetheless **intentionally
  not consulted for push-depth gating.** The reason is not forgeability — it is that the flat
  cap's entire value is being *unconditional*. Any human-presence input, even a reliable one,
  means a confused agent that happens to be inside a recency window gets its cap reset and
  spirals further before tripping. The cap is deliberately deaf to presence so that nothing
  can extend a spiral. `human_interacted` remains the right signal for what it already does:
  the chain-depth recency bypass *and* the push-suppression-filter release (§3) — both
  mechanisms that depend on knowing whether a human is plausibly at the terminal right now.
  It is simply never an input to the depth counter. This is a deliberate exclusion, not an
  oversight.
- The honest conclusion: **the server cannot know whether a human is currently engaged**, and
  even where it has a usable proxy, the cap does not consult it. There is no signal for it,
  and by design the cap wants none.

So the depth cap does not try to observe presence at all. It relies instead on something the
server *can* see with certainty: healthy conversations terminate on their own. A real work
exchange converges and ends after a handful of bounces; it never needs rescuing. A spiral
does not terminate — that is its definition. Therefore a flat cap, set above the length of
any healthy exchange, separates the two without any presence inference. A human message is
not needed even as a bonus: a user-tier message either lands on an existing chain (and is
just another message in it) or starts a new chain (depth 0 by construction). There is no
case where a separate "human reset" event is required, so the concept is removed entirely.

Because the agent never interacts with the counter, there is also no chicken-and-egg release
problem — which is what forced the A/B/C release-trigger options in the prior spec.

**Push budget and hard ceiling → per sender.** The realistic failure starts with *one*
malfunctioning agent. That agent may loop against a single receiver, or it may fan out —
a confused coordinator re-broadcasting a stale directive hits every team at once. It may
also spiral by sending many *new* messages — fresh chains, each at depth 0 — which the
per-thread depth cap is structurally blind to, since no single chain's counter moves. A
per-pair or per-receiver counter also misses the fan-out case: six messages to six teams
leaves every pair at 1. The common factor across every failure shape — symmetric loop,
fan-out, or many-new-messages spray — is one sender emitting too much. Counting the sender
catches all variants, and it is the cheapest possible key: one counter per agent.

This is the deliberate division of labor: the **depth cap** catches the deep single chain;
the **per-sender ceiling** catches the broad shallow spray that slips under the depth cap.
Neither alone is complete; together there is no gap.

**Cross-project sends are billed to the sender's home project.** Counts are partitioned by
project (§4), but a message can cross projects — `memory@junto` sending to a recipient in
`nimbus`. The sender's emission is debited against the **sender's** home project, not the
recipient's. The budget measures *how much this agent is emitting*, which is a property of
the agent; it belongs in the agent's own namespace. Billing the recipient's project would
defeat the per-sender design: a central agent that talks to every project would accumulate a
separate budget in each and be throttled in all of them — exactly the punish-the-hub failure
the per-sender scope was chosen to avoid.

**Counter key implementation.** Per the cross-project billing rule above, the per-sender
counter's key is `(sender_instance, sender_project)`. Because each agent identity belongs to
exactly one project, the per-project namespacing is cosmetic for any single-project
deployment — a junto-only deployment has all keys ending in `@junto`. The namespacing
matters for multi-project adopters who run several deployments and want isolation: an agent
named `coordinator` in `nimbus` does not share a counter with `coordinator` in some other
project, even if both project servers federate.

**System-generated messages are excluded from budget accounting.** `system@junto` notices
(§8) and any other server-synthesized messages do not count against any sender budget. They
are not sender emissions; they are server outputs whose existence is bounded by incident
frequency, not by sender behavior. Counting them would inflate the `system@junto` counter
pointlessly during a recovery flurry, and could in principle suspend the synthetic source
itself — defeating the purpose. Separately, system-generated messages are non-pushing by
construction (§8): they are written into the inbox via the ordinary message-write path but
are never selected for delivery via channel push. The two properties — not counted, not
pushed — together mean system messages cross the bus without engaging any push-control
machinery. This is the only legitimate exception to the principle in §11 that every send
increments the sender's counter.

**Rejected scopes.** *Total / project-wide:* conflates "the system is busy" with "the system
is spiraling" — five healthy concurrent conversations would trip it. *Per-pair / per-receiver:*
misses fan-out; punishes a central agent for being central.

---

## 7. The hard ceiling: suspend, don't instruct

When a sender hits the hard ceiling, the instinct is to message the agent "shut down and
wait for a human." Do **not** do this. The agent that hit the ceiling is, by definition, the
one that has proven it cannot reliably process its context. Sending an instruction *to the
broken thing* and hoping it complies has a failure mode: a confused agent may read "shut
down" as a task, or something to ack, or something to "fix" — and now it spirals about being
told to stop.

There is a real difference between *telling* the agent to stop and *making* it stop. The
agent only acts because the server pushes messages into its context and wakes it. So:

**When the hard ceiling trips, the server suspends pushing for that agent — both directions.**
No inbound message wakes it; its own outbound sends are accepted-and-persisted but never
pushed onward. The agent is not told anything. It simply has nothing arriving to react to,
runs out of inputs, and goes idle on its own. That is making it stop, with no dependency on
the broken thing behaving.

The notification still fires — it just goes to the entity that can act on it:

- **To the human** — dashboard / claudeControl alert. "Agent X — pushing suspended at 02:14,
  N messages in the prior hour." This is the message that matters; it goes to the human,
  because the human resolves it.
- **Alert delivery is out-of-band from the suspended push surface, by construction.** This is
  not optional plumbing: if `memory@junto` trips the ceiling and its bus is suspended, an
  alert routed over that same bus would be the one alert that never arrives — at the exact
  moment it is most needed. The alert path (dashboard, claudeControl, whatever external
  surface) must be independent of the message bus the incident just suspended.
- **The alert should carry enough to root-cause the incident**, since the realistic cause is
  usually a plumbing bug, not a confused model: which agent, what it was sending, and whether
  the messages were identical-repeating (→ plugin / delivery bug) or varied (→ genuine
  confusion). This turns each rare incident into a quick diagnosis instead of a mystery.
- **Optionally, to the receivers** — a quiet flag that messages from X are suspended, so they
  do not sit waiting on a reply that will never come. Nice-to-have; their inboxes simply stop
  growing either way.

**Channel implementation: persisted-alerts collection + best-effort webhook + polling
fallback.** Memory's process writes each alert to a separate `alerts` collection (distinct
from `messages` — the alert path shares no storage or transport with the suspended message
bus). Memory then POSTs the alert to a configurable webhook URL (default: claudeControl's
`POST /api/alerts/incoming`, authenticated via a shared static token in operator config).
The webhook is best-effort — failures do not lose the alert, since claudeControl polls
`memory_list_alerts(unacknowledged=True)` on startup and every ~60s for missed webhooks.
Durable plus timely; appropriate for a system that fires "a handful of times a year" (§10).
When memory is the agent that tripped its own ceiling, its MCP HTTP server (port 8080) is
still running — only the message-bus internals are suspended. The alert webhook fires from
memory's process via a separate outbound HTTPS call; bus suspension is a logical filter on
bus reads/writes, the process is unaffected.

**Alert lifecycle has two distinct states:** `alert.acknowledged` (operator has seen the
alert via the dashboard) and `agent.suspended` (the server is still gating that agent's
pushes). **Ack ≠ unsuspend.** Per §8, recovery is a fresh session the human triggers
manually, so an operator may ack an alert and continue investigating before triggering
recovery. The dashboard surfaces both fields independently.

**Required alert fields:**

- `agent` — the suspended agent (e.g. `memory@junto`)
- `trigger` — one of `depth_cap`, `push_budget`, `hard_ceiling` (operator response differs by trigger)
- `prior_hour_message_count` — count of sender's emissions in the hour leading up to the trip
- `window_start`, `window_end` — incident-window timestamps (per §8)
- `recipient_set` — list of recipient agents in the incident window (renders one-to-one vs fan-out shape at a glance)
- `shape` — structured `"identical_repeating"` or `"varied"` (operator response differs by class)
- `shape_explainer` — short text capturing how shape was determined (e.g. `"11/12 messages have identical SHA-256 of body"`)
- `sample_messages` — inline ~5 representative messages (≤50KB total at v0 volumes)
- `peer_notice_inserted` — bool, whether a `system@junto` notice was inserted into the peer's inbox (§8)

The agent that tripped gets nothing — not because it does not "deserve" to know, but because
no version of "telling it" is more reliable than "unplugging it," and the telling has a
failure mode the unplugging does not.

---

## 8. Recovery: annotated delivery

When the human has seen the alert and investigated, recovery is a fresh session for the
suspended agent — new context, reads its state spec, no memory of the spiral.

The question is what that agent — and the *other* endpoint of the spiral — finds in the
inbox. The default behavior is **annotated delivery**, which is a strict improvement over the
two obvious alternatives. Pure *quarantine* hides the data: the agent works blind. Pure
*leave-it* gives the data but no signal: the agent works without knowing some of its inbox is
suspect. Annotated delivery gives the agent the messages **and** the context to judge them.

### The incident window

Annotation operates on an **incident window** — the messages identified as belonging to the
spiral. The window's shape depends on which limit detected the incident, because the two
triggers detect two different failure geometries:

- **Depth-cap trip** — the failure *is* a single chain that would not terminate. The unhealthy
  unit is the whole chain, so the window is the **entire thread, depth 0 through the trip**.
- **Budget / hard-ceiling trip** — the failure is one sender emitting too much across possibly
  many chains. There is no single chain to color, so the window is a **look-back from the
  first soft-limit trip**, sized `min(depth_cap messages, 5 minutes real time)` of that
  sender's output.

In both cases the window extends *forward* from the trip to the hard-ceiling suspension.

The window opens at the *detection* point, which necessarily lags the true *onset* — an agent
may have been looping for a few messages before the count crossed the threshold. The
`min(depth_cap messages, 5 minutes)` look-back is a deliberate backward pad to catch that
run-up. The two bounds each cover a case the other misses: for a fast spiral the message
count caps coloring at ~12 messages (~1 minute of fast output); for a slow buildup the
5-minute bound caps coloring at ~1–2 messages on a low-traffic sender, rather than the half-
day that a pure 12-message look-back would span. The asymmetry is intentional: over-coloring
a few healthy pre-spiral messages just means the agent double-checks something that was fine;
under-coloring means it trusts something that was not.

### Notices fire only on hard-ceiling trips

Soft trips — depth-cap or push-budget — silently contain without escalation. There is no
recovery, no "incident-close" event, no notice. A depth-cap-only trip pauses the chain; the
human investigates if they care, and the chain remains paused either way. The
annotated-delivery machinery is specifically a **hard-ceiling recovery** mechanism: when an
agent has been suspended and is being restored to service, both endpoints of the spiral
receive the synthetic notice as part of that restoration. Soft trips need none of that
ceremony — they are routine soft containment, not incidents in the recovery sense.

### The notice

At incident-close, the server inserts a synthetic notice into the inbox of **both endpoints**
of the spiral — the suspended agent and its peer — positioned **immediately ahead of the
incident-window messages**, not at head-of-inbox. Pre-incident messages are not suspect;
coloring them would dilute the signal. The incident-window messages then flow normally, in
original order, after the notice. **Nothing is hidden.**

The notice is a message from `system@junto` — a server-generated source, not an agent. It is
written to be **self-contained and self-explanatory**: an agent with no concept of "spiral"
or "push control" must understand it completely from the text alone. It states, in plain
language: the incident-window timestamps, the agent that tripped, the count of suspect
messages, the shape of the incident (fan-out, thread loop, or rapid resends), and one
guidance line — that the messages below arrived during a suspected malfunction, may reflect
state from before it, and should each be evaluated for current relevance against the agent's
state spec before being acted on.

The notice is **non-pushing by construction** — it is a notice type that never wakes anyone.
It is meant to be *found* on the next normal inbox flush, sitting in front of the suspect
messages, not to trigger a session. It is inserted via the ordinary message-write path (the
same path every `memory_send_message` uses), so there is no new session-start code branch and
the notice is durable in the database from the moment the incident is detected.

### Why the peer gets a notice too

The suspended agent is recovered, reset, and warned. The *peer* — the other endpoint — was
never suspended and is very likely **not** malfunctioning. But its inbox now holds a pile of
spiral messages, and on its next session it will do normal inbox triage and reason about them
as ordinary requests, *in good faith, because it is functioning correctly and trusts its
inbox*. Without a notice, the healthy agent is the one most exposed to acting on spiral
garbage. So the peer is not collateral — it is a primary recipient of the notice, and for the
peer the notice is inserted at incident-close, ahead of the spiral messages, waiting for
whenever it next runs.

### Relation to §2

This is the one acknowledged point of contact between an agent and the push-control mechanism
(§2). It does not reintroduce the marker problem: the agent carries no standing rule, the
notice is a single after-the-fact factual message, and it appears only in a post-incident
recovery context. The agent reasons about a historical fact; it does not follow a push-control
rule.

### Configurable

Annotated delivery is the **default** recovery behavior. Two alternatives remain selectable
per §13 for operators who want them: **pure quarantine** (incident-window messages withheld
from the recovered session entirely, visible only to the human) and **pure leave-it**
(incident-window messages delivered with no notice). The default is annotated because it is
the only one of the three that loses neither the data nor the signal.

---

## 9. Interactive vs. non-interactive agents — a known boundary

Everything above quietly assumes an **interactive agent**: one Tom started in a terminal.
For an interactive agent, containment is naturally safe. When pushes stop, the agent
finishes its current turn, has nothing arriving, goes idle, and waits. An idle interactive
agent costs nothing while it waits, and recovery is simply Tom returning to the terminal.
This is the only mode Junto uses today, and the design is complete for it.

A **non-interactive agent** — headless, scheduled, or self-driving from a task loop — behaves
differently, and the design does **not** fully cover it:

- A non-interactive agent is not driven by pushed messages; it has its own task loop.
  Suspending its pushes contains the *messaging* spiral, but the agent keeps running its loop
  and keeps consuming model turns. If the spiral is internal (a confused task loop rather
  than a messaging loop), cutting pushes does not stop the cost.
- There is no terminal for Tom to return to, so "picked up on the next human-driven session"
  and terminal-based recovery do not apply.

The honest one-line statement of the limit: **the message server can stop messages; it
cannot stop an agent.** For interactive agents that is enough, because stopping the messages
makes them idle. For non-interactive agents it is not — stopping a self-driven spiral
requires a supervisor *outside* the message server (a launcher or job runner that owns
process termination), and the hard-ceiling alert would need to reach that supervisor.

**This is explicitly out of scope for v0 and marked as a known unsolved issue, to be
designed when Junto actually adopts non-interactive agent messaging.** Drawing the boundary
cleanly is deliberate: the message server handles messaging; process supervision is a
separate concern with a separate owner. Until that work is done, non-interactive agents
should not be run on the message bus without an external supervisor.

---

## 10. How often this fires

Rarely. With current Claude, the classic symmetric infinite loop is uncommon: a modern agent
reasons about each message as a discrete decision (Junto's own Phase A testing confirmed
this — channel messages arrive as untrusted context and the receiver evaluates rather than
auto-executes), recognizes when a conversation has converged, and with a large context
window rarely loses the thread mid-session.

The depth cap will likely trip occasionally and unremarkably — a plugin re-delivery, a
status-check that ping-pongs once too often — and just pause a chain. The hard ceiling should
trip *rarely* — perhaps a handful of times a year — and when it does, the cause is far more
likely a plumbing bug or a misconfiguration than a model "going insane."

What is trending up is not the per-incident probability but the **exposure**: more agents,
more orchestration, longer unattended runs. The features released this week (overnight
unattended work, multi-agent fan-out) are explicitly designed to run without a human
watching. Low probability × rising unattended duration is the combination this system is
insurance against.

This frequency reality is itself a design constraint: it justifies the simple model we have
and argues *against* adding a fifth mechanism, tuning the numbers paranoid-tight, or
revisiting this monthly. Build the simple version, set conservative numbers, mostly forget
it. If it fires twice a year and each time it is a plugin bug, the system is working — and
the fix is usually the bug, not the brake.

---

## 11. Relationship to `autopilot-delivery-filter-v0`

That spec is not implemented as written, but it is not discarded. It got one important thing
right and one thing over-built.

**Right, and kept:** gating belongs server-side; agents should not carry markers or autopilot
rules in their CLAUDE.md. The "concern bleed into the agent layer" complaint is legitimate.
This proposal honors that principle completely — and goes further, since the agent has no
awareness of the system at all.

**Over-built, and dropped:** the held-queue subsystem — a new schema column, a
`memory_get_held_messages` tool, a statusline badge, and a release-trigger with three
competing options (A/B/C) and a chicken-and-egg problem. That apparatus solved a problem one
size larger than the one Junto has.

Most of that spec's seven open questions simply evaporate under this model: there is no
agent-visible release-trigger tool, no held-queue surface tool (`memory_get_held_messages`
is not introduced), no statusline marker, no cutover-of-a-marker problem in agent prompts.
The marker-bleed concern is solved the cheap way: the server suppresses the *push* at
delivery time (the filter described in §3), the message sits in the normal inbox accessible
via direct query, no marker needed, no CLAUDE.md rule needed.

What this spec preserves from the prior design — and *must* preserve — is the
**delivery-time filter**: the server-side mechanism that excludes un-pushed messages from
the channel-poll response so that a continuously-polling plugin does not surface them as
live pushes. That filter has no agent-visible surface, no statusline indicator, and no tool
name. It is invisible infrastructure. It is *not* a held queue in the prior spec's sense (no
separate collection, no surface API); it just gates the channel-push delivery path while
leaving the underlying inbox unfiltered for direct queries.

The framing for `inbox` and `control`: *you correctly identified that gating belongs in the
server, not the agent — keep that. The held-queue subsystem solves a problem larger than the
one we have. Suppress the push, leave the message in the normal inbox. One flat per-thread
depth cap, one per-sender counter at two thresholds, one destructive gate. No presence
detection of any kind — see §6.*

The one open question from the prior spec that genuinely survives: **does an un-pushed
message count against the budget?** Yes — counted at the time the server decides not to push.
The budget measures how much a sender is emitting; an emission is an emission whether or not
it was pushed.

---

## 12. Terminology

- **Send** — a sender creates a message; always persisted, always succeeds.
- **Push** — the server places a message into the receiver's context and wakes it. The
  controlled action. "Push budget," "push depth cap," "pushing suspended."
- **"Autopilot" is removed.** There is no mode. The server makes one push/don't-push decision
  per message. Dropping the word also drops the false implication that the system has states.

"Push" is used because it is literally accurate — the server puts the message into context;
there is no receive, no fetch, no pull; the message is simply there and must be processed.
An engineer reading the code or spec gets the mechanic on first read with no metaphor to
decode.

---

## 13. Configuration

All tunable behavior is **operator configuration**, not contract and not code. It is layered:
a **server-level default** applies everywhere, and a **per-project setting overrides** the
server default when present. This gives a new adopter sensible behavior out of the box while
letting an operator who runs several projects tune each to its own rhythm — a low-traffic
design project and a heavy multi-team project need not share numbers.

All of it is **human-modifiable and operator-only**. Agents never read or set these values —
consistent with the core principle that agents are unaware of push control (§2). The
settings live with the server's existing operator/owner-tier configuration; there is no
agent-callable tool to adjust limits.

**CRUD surface: `memory_admin` sub-actions, owner-tier only.** The v0 CRUD path uses
`memory_admin` (the existing owner-tier tool) with new actions:

- `push_control_get_config(project=<name|null>)` — read server default + per-project overrides
- `push_control_set_config(project=<name>, key=<setting>, value=<value>)` — set or override one setting
- `push_control_reset_config(project=<name>, key=<setting|null>)` — drop a per-project override; null key drops all overrides for that project

claudeControl is the alert surface and the read-side dashboard (§5, §7); operator-grade
*write* paths are not built into claudeControl in v0 because claudeControl's runtime identity
is user-tier (`tom@claudecontrol`), and adding owner-tier write paths to the UI is a
non-trivial auth-model expansion. If usage frequency justifies it later (config edits
exceeding ~weekly), a claudeControl UI is a clean follow-on.

**The settings, with their defaults:**

| Setting | Default | Notes |
|---|---|---|
| Push depth cap | ~12 | Flat per-chain cap, no reset. Must clear any healthy multi-step exchange, including contract negotiations; raise it if a genuine long exchange is ever seen to hit it. |
| Push budget | 30 / sender / hour | Soft per-sender limit. |
| Hard ceiling | 100 / sender / hour | Per-sender suspension threshold; locked for junto deployment after empirical validation (max observed: 10/hr; 10× headroom). The ~100–150 range remains the recommended starting point for new adopters before they run their own pre-deployment analysis per the gate below. |
| Recovery behavior | `annotated` | One of `annotated` (default — incident-window messages delivered with a `system@junto` notice, §8), `quarantine` (incident-window messages withheld from the recovered session, visible only to the human), or `leave_it` (delivered with no notice). |
| Incident-window backward pad | `min(depth_cap msgs, 5 min)` | Look-back from the first soft-limit trip, for budget/ceiling-triggered incidents (§8). Depth-cap-triggered incidents color the whole thread instead. |

The numbers above are starting points derived from current usage. They are expected to be
retuned as agents are added or traffic patterns change — that is what the per-project
override is for. They are not guarantees and not an interface other components should depend
on.

**Pre-deployment validation (performed for junto + nimbus; required for new deployments
elsewhere).** The validation analysis was performed for this deployment on the junto + nimbus
message archive (2026-05-12 → 2026-05-19, n=383). Results: p99 chain depth 4 (junto) / 7
(nimbus), max 8 (nimbus); p99 sender hourly emission 8, max 10 (memory@junto). The numbers
locked above (12 / 30 / 100) reflect this analysis with comfortable headroom (50% over max
depth, 3× over max emission, 10× over max emission for the ceiling).

For any **new deployment in a different environment**, the same analysis is a deployment gate
before locking config — Nimbus's traffic pattern is not Junto's, and another project's
pattern is neither. Run one pass over that environment's message archive and check three
statistics:

1. **p99 healthy chain depth** — does the depth cap (~12) actually clear real conversations?
2. **p99 sender hourly emission** — does the budget (~30/hr) contain a busy-but-healthy
   sender without false-tripping?
3. **peak observed hourly emission** — is the hard ceiling (100, or higher in the ~100–150
   range for noisier deployments) above any legitimate traffic ever seen?

This is a deployment gate, not an open design question — the design is settled; this
confirms the *config values* before they are trusted in a new environment. Shipping a wrong
number does not break the design, but it produces silent false trips that erode confidence
in the whole system, so the check is not optional for new deployments.

**Known extension points (not in v0):**

- **Budget window** — a single hourly window is proposed. A second, longer rolling window
  (e.g. daily) could be added later if a slow-drip spiral that stays under the hourly limit
  ever appears in practice.
- **Non-interactive agent messaging** (§9) — a known unsolved boundary, deliberately out of
  scope until the system actually adopts non-interactive agents.

---

## 14. Explicitly rejected

- **Per-message-type limits.** Messaging has three *purposes* (peer-to-peer work; contract /
  interface changes; cross-cutting design questions) but they do not need three *limit
  regimes*. A confused loop is a loop regardless of what its messages are labeled; typing the
  limits just hands a runaway a branch to slip through. Message type changes *who is CC'd*
  (coordinator or not), not *how many pushes it gets*.
- **Any human-engagement reset of the depth counter** — whether by user-tier message,
  "human-attended session," or the `human_interacted=true` flag. The first two fail because
  the server cannot observe human presence; the third is a *reliable* signal in the safety
  model but is still excluded, because the cap's value is being unconditional — any presence
  input lets a confused agent in a recency window extend its spiral (see §6). The depth cap
  is a flat count instead.
- **Total / project-wide counting** — conflates a busy system with a spiraling one.
- **Per-pair / per-receiver budget** — misses the fan-out failure shape; penalizes central
  agents.
- **Denying the send / returning an error** — gives a confused agent something to fight and
  retry against; re-introduces a drop-shaped data loss the team already fixed once.
- **Messaging the malfunctioning agent to tell it to stop** — delivers an instruction to the
  one entity proven unable to process instructions correctly.
- **Held-queue subsystem** (per §11).

---

## Summary

A safety system, not a security system: it assumes agents that may *malfunction*, not agents
that are *adversarial*. The control layers are mechanical and model-agnostic — they contain a
confused agent regardless of vendor or who configured it, and that protection travels to any
deployment; hardened defense against an agent that deliberately games the brake is a
deliberate scope boundary, not part of this design. Agents are completely unaware of the
system. The server makes one decision per message — push or don't. A per-thread depth cap
catches the threaded loop with surgical isolation; a per-sender hourly budget catches the
loop or fan-out by counting the emitter; a higher per-sender hard ceiling suspends a
malfunctioning agent's pushing entirely and alerts the human. Soft limits contain silently —
accept, persist, don't push, never error. The hard ceiling unplugs rather than instructs.
Recovery is a fresh session; the spiral's messages are delivered to both endpoints annotated
with a plain-language notice of what happened, so the agents reason about them with full
context rather than blind or unwarned. The destructive-content gate is unchanged and
orthogonal. The whole thing is two counters and a handful of thresholds, costs nothing on the
happy path, and is insurance against a rare but unattended-and-expensive event.
