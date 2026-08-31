# Files: what FERAL may touch, and undoing what it wrote

FERAL cannot read or write anywhere on your machine by default. It can
only touch folders you have explicitly allowed, and everything it writes
is recorded so you can put it back.

## Allowing a folder

In the app: open the command palette with `⌘K`, type "folders", and pick
**Folders**. Type a path, choose whether FERAL may only read it or also
write to it, and press Allow.

From a terminal:

```
feral grant add ~/Projects          # readwrite by default
feral grant list                    # what is currently allowed
feral grant revoke ~/Projects       # take it back
```

Until you allow at least one folder, FERAL cannot read or write files
anywhere. That is the intended starting state, not a fault.

### Read only versus read and write

The Folders page shows these as two different things because they are.
Read means FERAL can look at your files. Read and write means it can
change and delete them. Grant the narrower one when that is all the task
needs.

## Undoing what FERAL wrote

Every turn where FERAL wrote or edited a file is recorded, with the
previous contents kept. So is every turn where it made a calendar event,
a reminder or a routine, which are undone by deleting them again. Open
the command palette and pick **Undo**, or:

```
feral checkpoints list              # turns that changed something
feral checkpoints show <turn>       # what a revert would do, before doing it
feral checkpoints revert <turn>     # put the files back
```

The page shows you what will happen before anything happens: which files
go back to their previous contents, which get deleted because the turn
created them, and which calendar events, reminders or routines get
deleted for the same reason.

`feral checkpoints revert` in a terminal puts files back but cannot
delete a calendar event: that needs your account, which the command
deliberately does not touch. It tells you what it left behind. Undoing
from the page, or by asking FERAL, does the whole job.

### If you changed a file yourself afterwards

The undo is refused, as a whole, and nothing is restored. Not even the
files you did not touch.

This is deliberate. A half-reverted turn leaves your folder in a state
neither you nor FERAL has ever seen. The refusal names the files that
changed, and offers to go ahead anyway, which discards your newer
edits. Nothing is lost unless you choose that.

### If somebody already deleted it

If you deleted the calendar event yourself before undoing the turn, that
is not an error. Undo notices it is already gone and moves on.

If FERAL cannot reach your calendar at all, that **is** an error, and it
says so: the files came back, the event did not, and it names which. It
never reports a half-finished undo as finished.

## What undo does not cover

Files written through FERAL's file tools are tracked, and so are calendar
events, reminders and routines it created. Nothing else is.

Anything a shell command changed is **not** recorded and will **not**
come back: shell redirects, `sed -i`, formatters, package installs, git
commands. The Undo page says so permanently on screen, and it is the
most important sentence on it.

If a turn ran a shell command that changed files, undo cannot help you.
Use your own version control.

Neither can it unsend. An email is gone the moment it is sent. A Slack,
Telegram, WhatsApp or iMessage message can sometimes be deleted, but only
for a while, only on some services, and never before the other person's
phone has already buzzed, so FERAL does not offer that as undo. A
purchase moves money and has no reverse.

## See also

- [approvals.md](approvals.md), for stopping a file write before it happens
- `feral doctor`, which reports the folders currently granted
