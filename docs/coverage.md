# Test coverage ratchet

Coverage is **enforced on every `pytest` and `vitest` run**, not a special
invocation. Regressions fail the PR. Raising the floor is a one-file change;
lowering it requires a commit-message justification.

## Current floors

| Surface | Tool | Floor | Evidence |
|---------|------|-------|----------|
| `feral-core/` | pytest-cov | 50% lines | `feral-core/pyproject.toml [tool.coverage]` |
| `feral-client-v2/` | vitest v8 | 33 / 26 / 27 / 35 (stmts/branches/funcs/lines) | `feral-client-v2/vitest.config.js` |

The backend gate is set just below the most recent measurement so a single
small regression blocks. The client-v2 thresholds sit roughly one point
below the most recent measurement so a single-comment edit cannot break
CI while a real regression (skipped page, broken hook) still trips the
gate.

## How to raise the floor

The rule of thumb is:

> After every commit that adds meaningful tests, check the new measurement
> and bump the floor to (measured − 1%). Never bump statements, branches,
> functions, and lines all at once; let the suite prove it.

Track-changes log lives in this file's git history — each `coverage.md`
edit since the gate was introduced bumps either the backend percent or
the per-axis client-v2 thresholds.

## What's under-covered

A few backend modules are routinely under-covered because they're heavy on
real I/O or third-party calls:

| Module | Reason |
|--------|--------|
| `skills/impl/system_settings.py` | Heavy side-effect code (filesystem + OS) |
| `skills/marketplace.py` | Needs integration harness |
| `skills/impl/weather.py` | Third-party API calls |
| `skills/package.py` | Tarball I/O |
| `voice/gemini_realtime.py` | Live WebSocket session needed |
| `skills/impl/workspace_scripts.py` | Spawns subprocesses |

Each follow-up commit that backfills one of these should bump the
corresponding entry up — or remove the row when the module clears 70%.

## How to check locally

```bash
# Backend
cd feral-core && pytest                # coverage runs by default
cd feral-core && pytest --no-cov       # opt out (dev / debugging only)

# Frontend (v2)
cd feral-client-v2 && npm test                 # tests, no coverage
cd feral-client-v2 && npm run test:coverage    # tests + v8 coverage gate
```

## CI

- `.github/workflows/ci.yml` → job `brain-tests`: `pytest --cov-fail-under=50`
- `.github/workflows/ci.yml` → job `client-v2`: `npm run test:coverage`

Both jobs block merge on regression.
