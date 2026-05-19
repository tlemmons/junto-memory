# Push Control — two-page summary

*A safety brake for inter-agent messaging in a multi-agent system. This is the short
version for engineers picking up the concept. The full design rationale — every rejected
alternative and why — is in the companion document `push-control-design-writeup.md`.*

---

## Part 1 — What inter-agent messaging is, and how it's used

If you run a single Claude agent (or an Open Claw / single-assistant setup), "inter-agent
messaging" may not map to anything you do. Here is the context this design lives in.

**The setup.** Junto is a multi-agent system. Instead of one general
assistant, there are several *specialist* agents, each with its own scope — on the Nimbus
project, for example, there are agents like `server-team`, `frames-team`, `infra-team`,
`jobs-team`, `mobile-team`, and a `coordinator`. Each runs as its own Claude Code session,
often on a different machine. They share a memory server (Junto) that
holds learnings, specs, a function registry, and — relevant here — a **message bus**.

**Why agents message each other at all.** The work crosses agent boundaries. The server
agent changes an API; the mobile agent consumes it. The infra agent runs a broker test;
the server agent needs the result to form a verdict. Rather than route everything through
a human, agents send each other messages directly. A message can carry a question, an
answer, a status check, a notification that a contract changed, or a hand-off of context.

**The three real uses, observed in production.** Across three months on Nimbus, messaging
settled into three purposes:

1. **Peer-to-peer work exchange** — the default and ~90% of traffic. One team asks another
   a domain question; the other answers. Bug investigations, "I changed X, you consume X,"
   status checks. Example: `server-team` and `infra-team` going back and forth over a
   broker out-of-memory test — one runs it, the other interprets the logs.

2. **Contract / interface changes** — when one agent changes a boundary another depends on
   (an API, a message schema, a database column), the `coordinator` is copied so the change
   is visible. These often need a human decision because they change a signed contract.

3. **Cross-cutting design questions** — the `coordinator` *frames* the trade-offs and
   forwards to the human. It explicitly does **not** decide on the human's behalf.

**The key property: most messaging is meant to flow without a human in the loop.** That is
the entire point — agents coordinate so the human doesn't have to relay. A message sent to
an idle agent *wakes* it: the agent reads the message and acts on it without anyone typing.
This is what makes the system useful, and it is also what creates the risk this design
addresses.

**The peer-review payoff (why it's worth the risk).** The reason to let agents talk freely
is not parallelism — it's that the agent *consuming* a piece of work pushes back on the
agent that *produced* it. A single agent reviewing its own work is a weak check; two agents
with a real interface between them catch each other's mistakes. Free-flowing messaging is
what enables that. The brake below is designed to preserve it, not throttle it.

**The risk.** An agent can malfunction — misread its state, think work is unfinished,
re-send. If it loops, and messaging flows without a human watching, it burns model-turn
usage and context budget on a conversation no one will read. On an unattended overnight run
that can be a lot of wasted usage. The push-control system is the brake for that.

---

## Part 2 — The push-control design

### The core idea: send vs. push

Two separate actions:

- **Send** — an agent creates a message. It is *always* persisted and always succeeds. The
  sender never sees an error or a limit.
- **Push** — the server places the message into the receiver's context and *wakes* it. This
  is the controlled action. The server decides, per message, whether to push.

A message that is sent but not pushed is **not lost** — it sits in the receiver's normal
inbox and is picked up the next time a human drives that agent. "Suppress, never drop."

**Agents are completely unaware of this.** No markers in their prompts, no counts in their
context, no rules to follow. An agent cannot tell "no message exists" from "a message
exists but wasn't pushed." The brake is entirely server-side. This is deliberate: an agent
cannot mishandle or drift away from a mechanism it has no contact with.

### The control layers

All counts are per-project. Within a project:

| Layer | Counts | Scope | At the limit |
|---|---|---|---|
| **Push depth cap** (~12) | Replies in one chain — a flat count, no resets | Per thread | Stop pushing that chain |
| **Push budget** (~30/hr) | Messages a sender emits per hour | Per sender | Stop pushing that sender |
| **Hard ceiling** (~100–150/hr) | Same sender count, higher line | Per sender | Suspend that agent's pushing entirely + alert the human |
| **Destructive gate** | Dangerous-keyword tripwire on content | Per message | Force human review |

Limits are server-level defaults, overridable per project, and modifiable by a human when
needed (operator-only — agents never see or set them). The numbers should be validated
against the message archive before they are locked for a given deployment. The recovery
behavior described below is likewise configurable; the default is annotated delivery.

A per-sender count is debited to the **sender's** home project, even for a message that
crosses into another project — the budget measures what the agent emits, which is a property
of the agent.

### Why these scopes

- **Depth cap is per thread** because a runaway loop *is* a single chain that won't end. A
  healthy exchange converges and ends after a few bounces — it seldom approaches 12. A chain
  that hits 12 unresolved replies should be checked, as that is unusual. The cap is a
  flat count with **no reset** (see "no presence detection" below).
- **Budget and hard ceiling are per sender** because a malfunction starts with *one* agent.
  It might loop with one partner, or fan out to many, or spray new messages. The common
  factor is always one sender emitting too much. The per-thread depth cap is blind to the
  "many new messages" spray (each new message is a fresh chain at count 0); the per-sender
  count catches exactly that. The two layers cover each other's blind spots.

### No presence detection — and why

An earlier version tried to reset the depth count whenever a human was "engaged" with a
chain. That was dropped. **The server cannot observe whether a human is present.** The
human starts every agent in a terminal (so every session looks "attended") but then walks
away — attendance fades and the server can't see it. Rather than guess at an unobservable
signal, the depth cap is a flat count: healthy conversations end on their own, so a
generous flat ceiling separates them from spirals without needing to know where the human
is.

### What happens at a limit

- **Soft limits (depth cap, budget): silent containment.** The server stops pushing. The
  message is still saved. The sender gets a normal success response — no error. This is
  deliberate: a confused agent given an error doesn't stop, it *retries*. A brake it cannot
  perceive is a brake it cannot fight.
- **Hard ceiling: suspend, don't instruct.** The malfunctioning agent has its pushing
  suspended both directions — nothing wakes it, its sends don't wake others. It is not
  *told* to stop (you can't reliably instruct a confused agent); it simply runs out of
  inputs and goes idle. An alert fires **to the human**, with enough detail to root-cause
  (which agent, repeating or varied messages — repeating usually means a plumbing bug).

### Recovery — annotated delivery

The human investigates, then restarts the agent fresh. The messages from the spiral are not
hidden and not delivered blind — they are delivered **annotated**. The server inserts a
plain-language notice (from a synthetic `system@junto` source) into the inbox just ahead of
the spiral messages, describing what happened: when, which agent, how many suspect messages,
the shape of the incident, and a line telling the agent these arrived during a suspected
malfunction and should be judged for current relevance before being acted on. The suspect
messages then follow normally.

Both endpoints of the spiral get the notice — the suspended agent *and* the healthy agent it
was spiraling into, since the healthy one has spiral messages in its inbox too and would
otherwise act on them in good faith.

This is the one deliberate point of contact between an agent and the control system, and it
is narrow: the agent carries no rule about push control; it just reads one after-the-fact
message describing a past event and reasons about it like any other inbox content. Operators
who prefer can configure pure quarantine (messages withheld) or pure leave-it (delivered with
no notice) instead.

### Honest scope notes

- **This is a *safety* system, not a *security* system.** It assumes agents that may
  *malfunction*, not agents that are *adversarial* — it catches a *confused* agent, not one
  deliberately gaming the brake. The control layers are mechanical and model-agnostic: they
  contain a confused agent regardless of which model or vendor it runs on, and that
  protection travels intact to any deployment. What does **not** travel is an assumption that
  the agents are trustworthy by construction. Junto today is a closed, single-operator
  environment where the realistic risk is genuinely confusion. An operator deploying this
  elsewhere inherits a real boundary: anyone placing genuinely *untrusted* agents on the
  message bus needs controls beyond this design.
- **It is rare-but-costly insurance.** With current models, runaway loops are uncommon;
  the realistic triggers are plumbing bugs and misconfiguration more than a model "going
  insane." The justification isn't frequency — it's that the guard is nearly free and an
  unguarded incident overnight is expensive.
- **Interactive agents only, for now.** The design assumes an agent a human started in a
  terminal — when pushes stop, it idles harmlessly. A *non-interactive* agent (headless,
  self-driving) keeps running its own task loop; suspending its messages doesn't stop it.
  The honest limit: *the message server can stop messages, it cannot stop an agent.*
  Non-interactive agent messaging is a known unsolved case, deliberately out of scope until
  the system actually needs it.

### The whole thing in one paragraph

Agents message each other freely; the server decides per message whether to *push* it into
the receiver's context. A flat per-thread depth cap catches a single chain that won't end.
A per-sender hourly count catches an agent emitting too much by any pattern. A higher
per-sender ceiling unplugs a malfunctioning agent and alerts the human. Soft limits contain
silently — accept, persist, don't push, never error. Recovery is a fresh session; the
spiral's messages are delivered to both endpoints annotated with a plain-language notice of
what happened, so the agents reason about them with context rather than blind. Agents never
know any of it exists. It is two counters and a few thresholds, costs nothing when things are
healthy, and exists to bound a rare but expensive failure.
