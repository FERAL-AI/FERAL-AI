# First run

```
feral setup
```

The wizard walks through an LLM provider, voice, who you are, and
optionally pairing a device. You can skip parts and come back.

Then:

```
feral start          # run in the background
feral status         # is it up
```

or `feral serve` to run it in the foreground and watch the log.

Open the dashboard at the address `feral start` prints.

## Finding your way around

The **dock** along the bottom holds eight places you go often. The **⌘**
tile at its right end opens the command palette, which is also `⌘K`.

The palette is the main way to get anywhere. It does three things:

- **Go**: every page, including ones with no dock tile
- **Do**: start a voice session, switch theme, start a new conversation
- **Ask**: type a question and hand it straight to the chat composer

If you cannot find something, press `⌘K` and type what you want. You do
not need to remember which page it lives on.

## Your first conversation

Go to **Chat** and type. FERAL answers, and when it uses a tool the call
appears as a card in the transcript: what it ran, how long it took, and
what came back.

Some calls stop and ask you first. See [approvals.md](approvals.md).

FERAL cannot read or write files until you allow a folder. See
[files.md](files.md).

## Threads

Conversations are saved. The pane lists them, and you can search, rename
and pin. A thread you rename keeps that name; it will not be overwritten
by whatever you happened to type first.

## Next

- [files.md](files.md), before asking FERAL to work on anything
- [voice.md](voice.md), to talk instead of type
- [devices.md](devices.md), to pair a phone
