# Pairing a phone or device

Home shows a **Pair** button when nothing is paired. The command palette
has the same action in its header.

```
feral access status         # current mode, and whether it can actually work
```

## The three modes

- **localhost**: the brain listens only on this machine. Nothing else
  can reach it, including your phone.
- **Same WiFi (LAN)**: reachable from devices on your network.
- **Remote**: reachable from anywhere, over a Tailscale Funnel.

```
feral access remote-up      # enable remote
feral access remote-down    # back to localhost
```

## Restart after changing the mode

The brain reads its listening address once, when it starts. Changing the
access mode while it is running saves the setting but does not move the
listener, so it would advertise an address nothing answers on.

FERAL refuses to hand out that address rather than giving you a QR code
that cannot work, and tells you a restart is needed. Restart the brain
after changing modes.

## If pairing hangs

`feral access status` first. It compares what you asked for against what
the running process is actually doing, which is the mismatch that causes
a phone to sit on "Connecting..." forever.

See [troubleshooting.md](troubleshooting.md).
