# Kickoff prompt for the Theora iOS agent

Copy the block below to start a session. It is deliberately an
exploration brief rather than a task list: the agent should do its own
homework against the source, because the handoff docs have been wrong
before and a plan built on a stale doc is worse than no plan.

Two things are load-bearing in how it is written:

* It demands `file:line` citations. Every claim in the handoff docs came
  from reading code, but some were wrong on first pass. Citations make
  the agent catch that rather than inherit it.
* Question 6 is a deliberate trap. It warns that something in the ambient
  area is misleadingly named and that an existing frame looks usable but
  is wired elsewhere, without naming either. An agent that finds them has
  actually read the code, which tells you how far to trust the rest.

---

```
You are working on the **Theora iOS app** and its connection to **FERAL**, a
local-first AI agent. FERAL is the "brain": it owns all state, all LLM calls,
all skills, all persistence. Theora (glasses + iOS app) is a "node": it owns
sensors and I/O and holds no durable truth. They talk over one WebSocket
using HUP, a typed JSON frame protocol.

Your job this session is to **understand the brain side well enough to decide
what Theora should do**, and to produce a plan. Do not start writing app code
until you can answer the questions below from the actual source.

## Start here, but verify everything

The FERAL repo has handoff docs written for you:

- `docs/handoff/FERAL_CORE_FOR_THEORA_IOS.md` - the map
- `docs/handoff/THEORA_FERAL_CONNECTION_REFERENCE.md` - wire detail and the
  ambient contract
- `docs/handoff/THEORA_IOS_TASKS.md` and `FERAL_COMPANION_IOS_TASKS.md` - open items
- `docs/handoff/WORKLOG.md` - what is actually done vs claimed, including
  corrections

**Treat all of these as a starting point, not as truth.** They were written by
another agent and some of them have already been wrong once.
`feral-core/models/protocol.py` is the authority on the wire format; if a doc
disagrees with it, the code wins, and tell me which doc is stale.

## Homework: answer these from the code, with file:line citations

1. **Transport and handshake.** Which socket does a node use, how does it
   authenticate, and what exactly comes back in `node_ack`? What happens if the
   brain denies a capability you asked for?
2. **The frame inventory.** What can Theora send today, and what can it receive?
   Which of those does the current iOS client actually decode? The gap between
   those two lists is the interesting part.
3. **Failure behaviour.** What does the app do with a frame type it does not
   know? What does it do when the socket drops mid-stream? Find the answer in
   the iOS code, not in a doc.
4. **Refusals.** A tool call can come back refused rather than failed. Find how
   that is represented on the wire and how the reference web client renders it.
   Decide what Theora should do.
5. **Health data.** There is a frame carrying Whoop and vitals. What is its
   payload shape, what are the two event types, and what would a renderer need?
6. **Ambient recording.** Establish what exists brain-side today. Be careful:
   at least one thing in that area is named misleadingly, and at least one
   existing frame looks like it would work but is wired to something else. Say
   what you find and what is genuinely missing.
7. **Voice.** How does the brain decide which voice provider to use, and what
   does the device need to send for that to come out right?

## The rule that matters most

**Never invent a frame on the device.** If Theora needs something the brain does
not send, the fix is a payload model in `protocol.py` plus a brain-side emitter.
Not a side channel, and never parsing prose out of a chat reply. That has gone
wrong before: the app once received health data only as English sentences inside
a chat response, which an app cannot render as a card.

If your plan needs a brain change, write it up as a proposed frame with its
payload shape and say so. Do not build the iOS half against an endpoint that
does not exist yet.

## Constraints

- **No em dashes** in code, comments, commit messages or docs.
- Never `git add -A`. Stage explicit paths.
- Never use real API keys. Never touch `~/.feral`.
- Do not push or commit unless I ask.
- If something is unverified, say so plainly rather than presenting it as fact.
  I would rather have "I could not confirm this" than a confident guess.

## Deliverable

A short findings document: what you verified (with citations), what the docs got
wrong, what the gap is between brain and app today, and a prioritised plan. Flag
anything where you think the current design is wrong, including anything I have
already decided.
```

---

## If you want a narrower session later

The same shape works for a single area. Replace the homework list with
one question, keep the "verify everything", "never invent a frame" and
"say so if unverified" sections, since those are what stop a confident
wrong answer.
