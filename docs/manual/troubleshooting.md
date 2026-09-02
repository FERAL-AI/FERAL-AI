# When something is wrong

## Start here

```
feral doctor
```

It reports what is actually true at runtime rather than what your
configuration claims, which is a different thing and the reason most
problems are confusing. It checks the interpreter, disk space, the
memory database, credentials, pairing reachability, local voice models,
and macOS permissions.

Read the severities as written:

- **✘** something core is broken and FERAL will not work until it is fixed
- **⚠** a real degradation of something you configured
- **ℹ** an optional feature you have not turned on, which is not a problem

A clean install shows several **ℹ** lines and no warnings. That is
correct and does not need fixing.

## The brain will not start

**"no such module: fts5"** means your Python was built without an SQLite
feature FERAL requires. `feral doctor` names the interpreter and the fix.
This is not something FERAL can work around; the memory store cannot be
created at all without it.

**Disk space** below 512 MiB free is refused outright, because at that
point writes start failing silently and the symptom is "everything got
slow" rather than an error.

## Voice produces nothing

Run `feral voice` to see which providers are configured and whether they
can actually run.

The common case is that a local engine is selected but its model was
never downloaded. Installing the package and downloading the weights are
two separate steps. `feral doctor` distinguishes them: "installed, no
model downloaded yet" is different from "not installed", and if you
selected a local engine that cannot run, it says so as a warning rather
than staying quiet.

If you chose local voice and a cloud provider is the only fallback
available, FERAL stops rather than sending your audio off the machine,
and tells you that is what happened.

## I cannot select text in `feral setup`

The wizard's lists are clickable, which means the terminal is reporting
mouse gestures to FERAL while a list is on screen instead of using them
to select text. Drag-to-select comes back with:

```
FERAL_SETUP_MOUSE=0 feral setup
```

Arrow keys and enter work the same either way. Many terminals will also
let you drag past a capturing application by holding shift (option in
iTerm2).

## The dashboard is blank or stale

The web UI is served by the brain from a bundle built at release time.
If it looks out of date after changing the source, the bundle needs
rebuilding; the running brain does not read the source directly.

## A phone will not pair

`feral access status` shows the current pairing mode and whether the
address FERAL is advertising is one anything is actually listening on.

The failure this most often catches: a pairing mode was set while the
brain was already running, so the setting changed but the listener did
not move. Restart the brain after changing access mode.

## Something FERAL was doing just stopped

Check **Needs you** in the command palette. A tool call may be waiting
on your decision. Nothing times out on its own. See
[approvals.md](approvals.md).

## FERAL changed a file it should not have

See [files.md](files.md). Note the limit: shell commands are not tracked
and cannot be undone.
