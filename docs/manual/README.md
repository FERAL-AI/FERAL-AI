# FERAL user manual

For people using FERAL. Nothing in here explains how it is built.

If you want the internals, they live elsewhere and stay there:
`docs/mintlify/architecture.mdx` for how the system is shaped,
`CLAUDE.md` for working on the code, `feral-nodes/HUP_SPEC.md` for the
device protocol.

That split is deliberate. Documentation that mixes "how do I pair my
phone" with a description of how memory synchronises between machines
serves neither reader.

## Pages

| Page | What it covers |
|---|---|
| [install.md](install.md) | Getting FERAL onto your machine, and what it needs |
| [first-run.md](first-run.md) | Setup, the dashboard, your first conversation |
| [files.md](files.md) | Which folders FERAL may read and write, and undoing what it wrote |
| [approvals.md](approvals.md) | Deciding on tool calls that are waiting for you |
| [voice.md](voice.md) | Talking to FERAL, cloud and local |
| [devices.md](devices.md) | Pairing a phone or other hardware |
| [troubleshooting.md](troubleshooting.md) | When something is wrong |

## The one command worth remembering

```
feral doctor
```

It reports real runtime state rather than what the configuration claims,
and every page here sends you back to it. If FERAL is misbehaving, run
it before anything else.
