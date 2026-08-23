# Install FERAL with your coding agent

For people who have Claude Code, Cursor, Codex, Copilot or similar, and
would rather not use a terminal directly.

Paste the prompt below into your agent. It will install FERAL, walk you
through setup, and verify the result. You do not need to know what any
of the commands do; the agent does, and the last step makes it prove
the install actually works rather than assuming it did.

---

## Paste this into your agent

```
Install FERAL on this machine for me. I am not a developer, so explain
what you are doing in plain language as you go, and stop and ask me if
anything needs a decision.

Follow these steps in order and do not skip the verification.

1. CHECK THE INTERPRETER FIRST.
   FERAL needs Python 3.11 or newer, AND that Python's SQLite must have
   the FTS5 extension. FTS5 is the usual reason an install looks fine
   and then the brain will not start. Check with:

       python3 -c "import sys, sqlite3; print(sys.version); sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)'); print('FTS5 OK')"

   If that errors, try python3.12 / python3.13 / python3.11 and use the
   first one that prints FTS5 OK. Tell me which one you found. Do not
   continue without one.

2. INSTALL.
   Run the official installer:

       curl -sSL https://raw.githubusercontent.com/FERAL-AI/FERAL-AI/main/scripts/install.sh | bash

   It creates a virtual environment at ~/.feral-env, installs the
   feral-ai package with all extras, and starts the first-run setup
   wizard.

   The installer itself never runs sudo, so do not run it with sudo. On
   Linux it may PRINT a suggestion like "Ubuntu: sudo apt install
   python3.12" if no suitable Python was found. That is a legitimate
   suggestion about installing Python, not the installer asking for
   root. Show it to me and let me decide, rather than running it
   yourself.

3. SETUP.
   The installer runs `feral setup`, an interactive wizard. It will ask
   for an LLM provider and an API key. I will provide the key when you
   reach that step, so pause and ask me for it rather than guessing or
   inventing one. Never put an API key in a file or a command you show
   me in plain text if you can avoid it.

4. VERIFY, and this is the step that matters.
   Run:

       feral doctor

   Read the output back to me and tell me plainly whether anything is
   red or yellow. Pay attention to these rows specifically:
     - "SQLite FTS5" must pass. If it fails the brain cannot start.
     - "Running version" tells you whether the running brain is the
       version that is installed. If it warns, the brain needs a
       restart, not a reinstall.
     - "Pairing & access" tells you whether a phone can actually reach
       this machine.

   Then confirm the brain is actually serving:

       curl -s localhost:9090/health

   If that returns nothing, the brain is not running. Start it with
   `feral start` and check again. Do not tell me the install succeeded
   until this returns something.

5. TELL ME WHAT TO DO NEXT.
   Give me the URL to open (it is http://localhost:9090), and tell me
   in one sentence how to start it again after I reboot.

Rules for you while doing this:
- Do not invent an API key, and do not use one from another project.
- Do not run anything with sudo.
- Do not delete or move any existing files.
- If a step fails, show me the actual error text rather than
  summarising it, and stop rather than trying a workaround.
```

---

## Why the verification step is in there

An install that finishes without an error is not the same as an install
that works. Two failures in particular look like success:

**A Python without FTS5.** `pip install` completes happily. The memory
store creates five FTS5 virtual tables at boot, so the brain then
refuses to start on that interpreter. Step 1 catches it before you
spend time on the rest.

**An upgrade that did not take.** A running brain holds its code in
memory and never reloads it, so upgrading the package on disk does not
change a process that is already serving. The install succeeds, nothing
errors, and the old build keeps answering. The "Running version" row in
`feral doctor` exists specifically to make that visible, which is why
the prompt asks the agent to read it out.

## Upgrading later

Same idea, shorter:

```
Upgrade FERAL for me.

First check whether `feral update` exists on this install (`feral
--help`). If it does, use it: it upgrades the same Python environment
that is actually running the brain and restarts it afterwards.

If it does not, do it manually and be careful which environment you
touch. Run `which feral` to find the one in use, upgrade with THAT
environment's pip (for the one-line installer that is
`~/.feral-env/bin/pip install --upgrade "feral-ai[all]"`), then run
`feral restart`.

Either way, finish by running `feral doctor` and reading me the
"Running version" row, to confirm the restart actually took effect.
```

The environment matters: more than one Python on a machine can each
hold their own copy of FERAL, and upgrading the one that is not running
is the common mistake. `which feral` tells you which one you are about
to touch.
