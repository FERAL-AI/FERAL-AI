# Approvals: things waiting on your decision

Some tool calls stop and wait for you rather than running. When that
happens the request sits in one place, whatever raised it.

Open the command palette with `⌘K` and pick **Needs you**.

## Why this page exists separately from chat

A tool call can be raised by something other than the conversation you
are looking at: a scheduled routine, a message from a channel, your
phone. Those have no chat window to appear in.

Before this page, such a request had nowhere to go. It waited, and
nothing on screen said so. Now every blocked call lands here regardless
of what raised it, and the row tells you which surface it came from.

## What a row tells you

- **The call itself**, tool and its main argument, so you can see what
  it will actually do.
- **Where it came from**: this chat, a scheduled routine, a channel
  message, your phone.
- **How long it has waited.**
- **Why you are being asked**: what the skill declared about itself, and
  what the danger map says. A call can be flagged because the skill's
  author marked it as needing approval every time, or because the
  operation is inherently destructive.

Rows are colour-coded on the left edge, and the words say the same thing,
so the severity does not depend on seeing colour.

## Deciding

**Approve** runs the call. **Decline** does not. Either way the row
leaves the list immediately.

Nothing times out on its own. A request waits until you answer it, which
is why it is worth checking this page if something FERAL was doing seems
to have stopped.

## See also

- [files.md](files.md), for undoing a file write you approved and regret
- `feral doctor`, for whether the brain is reachable at all
