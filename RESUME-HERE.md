# FERAL, state at 2026-08-21 end of session

Everything is committed and pushed to `main`. **No release tag is cut.**
Read "The one open thing" before doing anything else.

## The one open thing: CI is red, so the release is not published

`main` has all the work. The tag is what triggers the PyPI publish, and
the Linux lane of `ci.yml` fails, so it was deliberately not pushed.

This is NOT new breakage. The two main runs before any of this work
failed identically (2026-08-20, 2026-08-17), and every recent branch run
is red too. The job runs `pytest -x`, so each failure hides the next and
every fix costs a full CI cycle to find the one behind it.

Fixed so far, both merged:

1. **CI never installed ripgrep.** `coding_tools`' grep has two engines
   and the parity suite runs both. The runner had no `rg`, so the
   ripgrep leg produced the fallback and failed `assert
   'python-fallback' == 'ripgrep'` at 24%, halting the matrix. Now
   installed, and `FERAL_REQUIRE_RIPGREP=1` makes CI fail loudly rather
   than silently skip that leg.
2. **A macOS status asserted flatly on Linux.** `test_macos_ax.py`
   expected 404 and got 501, because `_snapshot` resolved the app
   (platform-gated) BEFORE validating arguments. Validation now runs
   first, which is also better behaviour: a bad `filter` is a bad
   `filter` on any platform.

Next failure is unknown. Get it with:

    gh run list --branch main --workflow ci.yml --limit 1
    gh run view <id> --log-failed | grep "pytest Linux matrix (3.11)" \
      | grep -E "FAILED|E   "

Do not try to find them all by patching `platform.system()` and running
the suite: it breaks collection with 19 errors. A Linux container would
work; colima is installed but was not running.

`mypy` also fails but is `continue-on-error` and has a known ~683-error
baseline. Ignore it.

## Do not publish until that is green

Version is already bumped to **2026.8.13** and `sync_versions --check`
reports drift 0. CHANGELOG has a full entry. So the release is one
command once CI is green:

    git tag v2026.8.13 && git push origin v2026.8.13

2026.8.12 is already on PyPI and the PRODUCTION publish step has no
`skip-existing` (only the TestPyPI canary does), so the version must not
be reused.

## Local state, all green

| suite | result |
|---|---|
| brain pytest (macOS) | 9813 passed, 0 failed |
| client vitest | 1136 passed / 144 files |
| e2e vs `vite preview` | 65 passed |
| e2e vs a LIVE BRAIN | 65 passed |
| ruff | clean |
| em dashes added | 0 |

Note the e2e suite had NEVER been green against a real brain before this
work. CI runs it against `vite preview`, which serves index.html for any
unknown path and therefore cannot see a route collision.

To reproduce the live-brain run:

    cd feral-core && FERAL_HOME=<scratch> python -m uvicorn api.server:app --port 9433
    curl -X POST localhost:9433/api/setup/complete -H 'Content-Type: application/json' \
      -d '{"settings":{},"credentials":{},"identity":{}}'      # else it bounces to /setup
    cd feral-client-v2 && FERAL_E2E_URL=http://127.0.0.1:9433 npx playwright test

Playwright's browser was wiped by a dependabot bump; run
`npx playwright install chromium` first.

## What was built this session

**Brain**

- Silence no longer reaches STT. 60s of digital silence produced 4 calls
  of 387,200 bytes with zero non-zero samples. Whisper hallucinates on
  silence, so the brain answered an empty room every 12 seconds.
- The test named after that bug ran 6.0s against a 12.0s ceiling.
- The credential sweep could never run: migrations run before
  `state.init()` but the plaintext file is written later in the same
  boot. Four boots later the key was still on disk with doctor saying
  "up to date". Now RECURRING.
- `/skills` and `/health` served raw JSON to a browser, shadowing two
  SPA routes. Content negotiation, with the rule resting on `Accept`
  because `sw.js` reissues the navigation and Chromium rewrites the
  Sec-Fetch metadata.
- **Ambient digest return leg** (the Theora spec). Persisted as
  `digest_json`, two frames, push on completion plus pull on connect.
  Three deviations from the spec, all deliberate: scoped to the
  authenticated device (transcript_id is client-supplied, so unscoped
  any paired node could read another device's conversation), capped at
  64 ids with `include_detail` off (512 x 20k detail is a ~10MB burst on
  reconnect), and `remaining` on the frame so a phone back after a week
  can show progress instead of appearing to hang.

**Client**

- The press-and-hold dock stack was painted and completely inert
  (`pointer-events: none` inherited from `.v2-dock`).
- The docked voice pill covered the dock on every screen under 1317px.
- Focus landed inside an `aria-hidden` subtree on all 28 routes.
- Fullscreen voice was an inescapable modal.
- Two guards in `VoiceLane.test.jsx` grepped for selectors that had
  already been deleted, so they could never fail.
- **The system bar was reading fields that do not exist.**
  `/api/dashboard` has no `cost_today`, `spend_today`, `tokens_used` or
  `autonomy`. The budget lived only on `LLMProvider._budget_snapshot()`
  with no HTTP surface; the tier only on `GET /api/autonomy`. Both now
  ride the dashboard payload. Every vital opens a popover you act from.
- Rail collapses with a control and with `B`. Rail rows have glyphs.
- Six system-bar controls were under the 24px WCAG target floor.
- Tertiary text missed AA in light mode (4.46:1 measured against the
  real composited ground).
- The Channels card rendered the response envelope as channels
  (`Active_channels off / Channel_count off / Details off`).

## Things to know before touching anything

**Home was NOT changed.** `/` still routes to `<Home />` (`App.jsx:67`)
and the total diff to `Home.jsx` this session is +33/-1 lines, all of it
the `channelMap` fix. Briefing/Desk/Wind-Down, Skills, In-flight, Right
now, Ask your Digital Twin, ForYouToday, ConnectedHardware and
ResumeCockpit (Consciousness) are all still there. If a running install
shows the old chrome, it is serving an old bundle: rebuild with
`bash scripts/build_webui_v2.sh` and restart the brain.

**`feral-client/`** at the repo root is 241MB of leftover v1 artifacts,
untracked and NOT gitignored. Never `git add -A` here.

**Agent worktrees get cut from `origin/main`, not the branch tip.** All
four audit lanes started 57-60 commits stale. Make base verification the
first required action in every brief.

**The user wants file edits through Read/Edit/Write, not shell
heredocs.** They want to see the changes.

## Still open, not started

- Three "end voice" controls on screen at once. The lane renders only on
  `/chat`; the overlay covers the other 27 routes and is the only
  surface with the degraded/quota banner. Which one owns "end" on
  `/chat` is a design call.
- `pip install feral-ai` ships a broken v2 dashboard: `webui_v2` has 69
  files on disk, the wheel carries 7. All 59 KaTeX fonts and all 3 PWA
  icons are dropped. Not a regression.
- Supervisor race, ~0.9% flake over 111 runs: `_monitor` cancels the
  consumer tasks then gathers them under a comment claiming they drain
  the queue. Cancelled tasks drain nothing, so a caller can see
  `status: completed` with silently lost output.
- `/approvals` is polled by three components.
- `remove_key`/`store_key` case normalisation (needs a migration).
- CLAUDE.md counts are stale: says 138 files/1067 tests (live 144/1136),
  7 spec files/30 tests (live 12/65), 949 .py (live 1060).
- 594 pre-existing em dashes in `feral-client-v2/src`. Nothing in CI
  enforces the rule.

## The user's live install looked unhealthy

Screenshots from `localhost:9090` showed `BRAIN reconnecting…`,
`Failed to fetch /api/conversations/save`, `Failed to fetch
/api/conversations/new`, and `503 on /api/conversations/active/thread`.
Unrelated to this work and worth diagnosing.
