.PHONY: install dev dev-python dev-brain dev-deps dev-verify dev-reset guard-python-version serve client docker docker-down test test-py test-py-ci test-client e2e lint clean clean-uv setup doctor bundle-webui docker-logs help

# ── Pinned development interpreter ───────────────────────────
#
# WHY THIS EXISTS: FERAL's SQLite needs two build-time features, and
# stock macOS interpreters ship one or the other, never reliably both.
#
#   1. FTS5. memory/store.py and memory/knowledge_graph.py create five
#      `CREATE VIRTUAL TABLE ... USING fts5` tables during construction,
#      so an interpreter without FTS5 does not degrade, the brain fails
#      to boot. Since the guard added alongside this pin it fails with a
#      readable SQLiteFeatureError instead of a bare
#      `sqlite3.OperationalError: no such module: fts5`, but it still
#      does not run.
#   2. Loadable extensions, for sqlite-vec. pyenv's default macOS build
#      omits --enable-loadable-sqlite-extensions, so sqlite3.Connection
#      has no .enable_load_extension at all. `pip install sqlite-vec`
#      still succeeds and `import sqlite_vec` still succeeds, so nothing
#      looks wrong; sqlite_vec_available() just returns False, logs at
#      INFO, and the vector leg runs over numpy. Optional, not fatal, and
#      per F-17 the numpy path is the faster of the two.
#
# Measured on this machine (macOS arm64):
#   pyenv 3.11.11                    sqlite 3.51.0  fts5=yes  load_ext=no
#   python-build-standalone 3.11.13  sqlite 3.49.1  fts5=no   load_ext=yes
#   python-build-standalone 3.11.15  sqlite 3.53.1  fts5=yes  load_ext=yes  <- pin
#
# WHY THE PIN IS IN .python-pin AND NOT .python-version:
#
#   pyenv reads `.python-version`. A repo-root `.python-version` naming an
#   interpreter pyenv does not have does not fail loudly, it makes pyenv's
#   shims fall through, so every bare `python3`, `ruff`, `pytest` and
#   `pip` run anywhere inside the tree silently becomes some unrelated
#   interpreter. Observed here: `python3 -c "import aiosqlite"` inside the
#   repo resolved to Homebrew 3.14.2 and raised ModuleNotFoundError, and
#   `ruff --version` exited 127 with "pyenv: ruff: command not found".
#   uv reads .python-version too, but nothing forces us to use that file,
#   and .python-pin is read by uv only because the Makefile passes it.
#
# uv resolves versions against a manifest baked into its own binary, and
# every 3.11 that uv 0.7.x can reach is from the pbs generation that
# shipped FTS5 off. scripts/ensure_uv.sh guarantees a uv that knows pbs
# release 20260807, downloading a repo-local one if necessary.
#
# See CLAUDE.md "The interpreter: pinned for dev, bundled for users".

VENV   := $(CURDIR)/.venv
PIN    := $(shell cat $(CURDIR)/.python-pin 2>/dev/null)

# Resolved inside recipes, never at parse time: ensure_uv.sh can download,
# and `make help` must not reach the network.
ENSURE_UV := bash $(CURDIR)/scripts/ensure_uv.sh

# Interpreter resolution, in order:
#   1. PYTHON=... from the command line or environment. Escape hatch that
#      preserves the pre-pin behaviour for anyone with a working setup.
#   2. the pinned venv, once it exists.
#   3. python3 on PATH. Unpinned legacy fallback, used before `make dev`.
ifeq ($(origin PYTHON),undefined)
  PYTHON_OVERRIDE :=
  ifneq ($(wildcard $(VENV)/bin/python),)
    PYTHON := $(VENV)/bin/python
  else
    PYTHON := python3
  endif
else
  PYTHON_OVERRIDE := 1
endif
PIP := $(PYTHON) -m pip

# The block above resolves at parse time, before dev-python has had a
# chance to create $(VENV). Recipes that run after it must re-resolve in
# the shell, or a first `make dev` on a clean clone installs into the
# machine's python3 despite having just built the pinned venv.
# An explicit PYTHON=... still wins, which is what makes the escape hatch
# real rather than decorative.
PY_RESOLVE = py="$(PYTHON)"; \
	if [ -z "$(PYTHON_OVERRIDE)" ] && [ -x "$(VENV)/bin/python" ]; then \
	    py="$(VENV)/bin/python"; \
	fi

# Recursively expanded on purpose: re-resolved at each use, so a venv
# created earlier in the same `make` run is picked up by later targets.
FERAL = $(shell [ -x "$(VENV)/bin/feral" ] && echo "$(VENV)/bin/feral" || echo feral)

# ── Tier 1: Quick start ──────────────────────────────────────

install:
	$(PIP) install -e "feral-core[llm]"
	@echo ""
	@echo "  Run: feral setup   (first-time configuration)"
	@echo "  Run: feral start   (brain + dashboard)"

setup:
	$(FERAL) setup

# ── Tier 3: Full development environment ─────────────────────

dev: dev-brain dev-deps dev-verify
	@echo ""
	@echo "  Brain deps installed with the same extras CI uses (all,dev)."
	@echo "  Client deps installed."
	@echo ""
	@echo "  Start developing:"
	@echo "    make serve     - start the brain"
	@echo "    make client    - start the web UI (separate terminal)"
	@echo "    make test      - run tests"

# Materialise the pinned interpreter. Deliberately a no-op once .venv
# exists, so a warm machine re-downloads nothing: uv caches the
# interpreter under ~/.local/share/uv/python and this target only shells
# out to uv (and to ensure_uv.sh, which can reach the network) when the
# venv is missing.
#
# A version-mismatched venv stops the build and is never deleted here.
# Silently blowing away a developer's environment is worse than telling
# them to, and continuing into it is worse than both: the whole point of
# the pin is that an unknown interpreter may not boot the brain at all.
# `make dev-reset` is the one command that removes it, and it says so.
#
# --seed is required, not cosmetic: `uv venv` omits pip by default, and
# `make install` / `make test` still go through $(PIP) = $(PYTHON) -m pip.
# Without it those targets break the moment .venv exists.
dev-python: guard-python-version
ifneq ($(PYTHON_OVERRIDE),)
	@echo "  [skip] PYTHON=$(PYTHON) given - not building the pinned .venv."
	@echo "         You are opting out of the interpreter pin; see CLAUDE.md."
else
	@if [ -z "$(PIN)" ]; then \
	    echo "  [error] .python-pin is missing or empty at $(CURDIR)."; \
	    echo "          It should contain a single version, e.g. 3.11.15."; \
	    exit 1; \
	fi
	@if [ -x "$(VENV)/bin/python" ]; then \
	    have=$$("$(VENV)/bin/python" -c 'import platform; print(platform.python_version())'); \
	    if [ "$$have" != "$(PIN)" ]; then \
	        echo "  [error] $(VENV) is Python $$have but .python-pin pins $(PIN)."; \
	        echo "          Continuing would install into an interpreter that may"; \
	        echo "          not boot the brain. Rebuild it with:  make dev-reset"; \
	        exit 1; \
	    fi; \
	    echo "  Pinned interpreter already present: Python $$have at $(VENV)"; \
	    exit 0; \
	fi; \
	uv=$$($(ENSURE_UV)) || exit 1; \
	echo "  Creating pinned dev environment (Python $(PIN)) at $(VENV)"; \
	"$$uv" python install "$(PIN)" || exit 1; \
	"$$uv" venv --seed --python "$(PIN)" "$(VENV)" || exit 1
endif

# A leftover `.python-version` anywhere in this tree re-creates exactly
# the breakage the pin was moved out of it to avoid: pyenv shims fall
# through and every bare `python3` / `ruff` / `pytest` in the repo runs
# under an interpreter nobody chose. Refuse to build on top of that
# rather than produce an environment that works only through `make`.
guard-python-version:
	@if [ -e "$(CURDIR)/.python-version" ]; then \
	    echo "  [error] $(CURDIR)/.python-version exists."; \
	    echo "          pyenv reads that file. If it names an interpreter pyenv"; \
	    echo "          does not have installed, every bare python3/ruff/pytest"; \
	    echo "          inside this repo silently resolves to something else"; \
	    echo "          (observed: python3 -> Homebrew 3.14.2, ruff -> exit 127)."; \
	    echo "          FERAL pins its interpreter in .python-pin instead."; \
	    echo "          Run:  rm $(CURDIR)/.python-version"; \
	    exit 1; \
	fi

# Explicit, never automatic. Deleting a contributor's environment as a
# side effect of an unrelated target is worse than making them ask.
dev-reset:
	rm -rf $(VENV)
	@$(MAKE) dev

# Run FERAL's own probes against whatever interpreter we ended up on and
# say so out loud, using the same memory.sqlite_features module the brain
# and `feral doctor` use, so this cannot report something the runtime
# disagrees with.
#
# FTS5 is a hard failure here, not a warning. `make dev` claiming success
# on an interpreter where `MemoryStore(...)` raises at construction is the
# exact surprise this whole change exists to remove. Loadable extensions
# stay informational: F-17 measured the numpy vector path as faster, so
# their absence costs resident memory and nothing else.
#
# PYTHON=... opts out of the pin by design, so it downgrades the FTS5
# failure to a warning: someone who deliberately pointed at their own
# interpreter has been told, and should not be blocked from working on
# parts of the tree that never touch memory.
dev-verify:
	@$(PY_RESOLVE); \
	cd feral-core && "$$py" -c "\
import sys;\
from memory.sqlite_features import interpreter_sqlite_report;\
r = interpreter_sqlite_report();\
print('  interpreter : %s (Python %s)' % (r['executable'], r['python_version']));\
print('  sqlite      : %s' % r['sqlite_version']);\
print('  fts5        : %s' % ('OK' if r['fts5'] else 'MISSING - the brain will not start on this interpreter'));\
print('  loadable ext: %s' % ('OK' if r['loadable_extensions'] else 'absent - sqlite-vec cannot load, vector search runs over numpy (faster; costs RAM on a large store)'));\
sys.exit(0 if r['fts5'] else 3)" \
	    && echo "  Environment verified." \
	    || { rc=$$?; \
	         if [ "$$rc" = "3" ] && [ -z "$(PYTHON_OVERRIDE)" ]; then \
	             echo "  [error] This interpreter has no SQLite FTS5, so MemoryStore and"; \
	             echo "          KnowledgeGraph cannot create their virtual tables and the"; \
	             echo "          brain will not boot. Expected the pinned $(PIN)."; \
	             echo "          Run:  make dev-reset"; \
	             exit 1; \
	         elif [ "$$rc" = "3" ]; then \
	             echo "  [warn] PYTHON=$(PYTHON) has no SQLite FTS5. The brain will not"; \
	             echo "         boot on it. You opted out of the pin; see CLAUDE.md."; \
	         else \
	             echo "  [warn] could not run the interpreter probe (deps not importable?)"; \
	         fi; }

# EXTRAS AND CONSTRAINT BOTH MATCH CI EXACTLY:
#
#   cd feral-core && pip install --constraint requirements.lock -e ".[all,dev]"
#
# --constraint requirements.lock means a contributor resolves the same
# versions across the extras instead of whatever pip picks on the day they
# clone.
#
# [all,dev] rather than [llm,dev]: with [llm,dev] the local suite does not
# match CI. `tests/test_doctor_severity.py` asserts a clean install emits
# zero warnings and no "Suggested fixes:", and without the [all] extras
# `feral doctor` legitimately warns "Playwright (driver lib) not
# installed", so two tests fail on a freshly built dev environment. That
# is exactly the kind of surprise `make dev` exists to remove: a green CI
# and a red local run, caused by the environment rather than the code.
# Verified that [all,dev] installs on the pinned 3.11.15 with no source
# builds, and that both tests pass under it.
DEV_EXTRAS := all,dev

dev-brain: dev-python
	@$(PY_RESOLVE); \
	echo "  Installing feral-core[$(DEV_EXTRAS)] into $$py"; \
	if uv=$$($(ENSURE_UV) 2>/dev/null); then \
	    "$$uv" pip install --python "$$py" \
	        --constraint feral-core/requirements.lock -e "feral-core[$(DEV_EXTRAS)]"; \
	else \
	    "$$py" -m pip install \
	        --constraint feral-core/requirements.lock -e "feral-core[$(DEV_EXTRAS)]"; \
	fi

# Client dependencies, including the browser Playwright needs.
#
# feral-client-v2 is the client the brain serves and the one every current
# test targets, and it was missing from this target entirely. So was the
# Playwright browser download: `@playwright/test` is a declared devDependency,
# but npm install does not fetch the browser binaries, and without them every
# e2e spec fails to launch. That is why the suite quietly grew a habit of
# passing `channel: 'chrome'` to borrow whatever Chrome the machine happened
# to have. A test that only runs on one developer's laptop is not a test.
#
# `playwright install --with-deps chromium` is the documented one-shot. It is
# skipped rather than fatal when npx is unavailable, because a contributor who
# only touches Python should not be blocked on a 92MB download.
dev-deps:
	@for dir in feral-client-v2 feral-client; do \
		if [ -d "$$dir" ] && command -v npm >/dev/null 2>&1; then \
			echo "  [deps] $$dir"; \
			(cd "$$dir" && npm install) || exit 1; \
		else \
			echo "  [skip] $$dir npm install (npm not found or directory missing)"; \
		fi; \
	done
	@if [ -d feral-client-v2 ] && command -v npx >/dev/null 2>&1; then \
		echo "  [deps] playwright chromium"; \
		(cd feral-client-v2 && npx playwright install chromium) \
			|| echo "  [warn] playwright browser download failed; e2e specs will not run"; \
	else \
		echo "  [skip] playwright browser download (npx not found)"; \
	fi

# Browser-level tests. Separate from `make test` on purpose: they need a real
# browser and a built bundle, so they are slower and have a hard dependency
# `make test` does not. They cover what jsdom structurally cannot see, which is
# layout, scroll containment, overlap, focus rings and painted colour.
#
# The two preconditions are checked rather than assumed, because both fail in
# ways that are easy to misread:
#
#   1. No chromium: playwright exits with "Executable doesn't exist at
#      .../chromium-*/chrome" per spec. Thirteen identical launch errors read
#      as a broken suite, not a missing download. `make dev-deps` fetches it.
#   2. No dist/: playwright.config.ts starts `npm run preview`, which serves
#      dist/. Without a build, preview serves nothing and every spec times out
#      waiting for the baseURL. That reads as a hang, not a missing build.
#
# These are hard errors, not warnings. An e2e target that cannot launch a
# browser must not exit 0.
e2e:
	@if [ ! -d feral-client-v2/node_modules/@playwright ]; then \
	    echo "  [error] @playwright/test is not installed in feral-client-v2."; \
	    echo "          Run:  make dev-deps"; \
	    exit 1; \
	fi
	@if [ ! -d feral-client-v2/dist ]; then \
	    echo "  [info] feral-client-v2/dist is missing; building it first."; \
	    (cd feral-client-v2 && npm run build) || exit 1; \
	fi
	cd feral-client-v2 && npm run e2e

serve:
	$(FERAL) serve

client:
	cd feral-client && npm run dev

# ── Docker (semi-manual tier) ────────────────────────────────

docker:
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "  Created .env from .env.example — edit it with your API keys."; \
	fi
	docker compose up -d --build
	@echo ""
	@echo "  Brain:    http://localhost:9090"
	@echo "  Client:   http://localhost:3000"
	@echo "  Registry: http://localhost:8080"

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

# ── Testing & quality ────────────────────────────────────────

# Both suites, because the client is half the product and `make test` used to
# run only the Python side. A contributor who changed a page and ran `make
# test` got a green result that had not executed one line of their change.
test: test-py test-client

# pytest-randomly is a [dev] dependency, so it is ACTIVE here and shuffles
# collection order with a fresh seed on every run. That is deliberate (it is
# how order-dependent bleed gets surfaced before CI, per the policy note in
# feral-core/pyproject.toml) and CI deliberately runs the other way, with
# `-p no:randomly`, so a CI result is reproducible and bisectable.
#
# What was broken is the pairing of that shuffle with `-q`: pytest suppresses
# the header under `-q`, and the header is the only place pytest-randomly
# prints "Using --randomly-seed=N". So this target could fail on an order
# dependency and discard the one piece of information needed to reproduce it.
# Re-running just drew a different seed and usually came back green, which
# teaches people that a red `make test-py` means nothing.
#
# The seed is now chosen here and echoed, so every run is reproducible:
#   make test-py PYTEST_SEED=1754000000
# The randomness is unchanged; only its traceability is fixed.
test-py:
	@seed="$${PYTEST_SEED:-$$(date +%s)}"; \
	echo "  pytest-randomly seed: $$seed"; \
	echo "  reproduce this exact order with:  make test-py PYTEST_SEED=$$seed"; \
	echo "  run in CI's deterministic order with:  make test-py-ci"; \
	cd feral-core && $(PYTHON) -m pytest tests/ -q --no-cov --randomly-seed="$$seed"

# The order CI actually runs. Use this to confirm a failure is real rather
# than order-dependent, and to reproduce a CI result locally.
test-py-ci:
	cd feral-core && $(PYTHON) -m pytest tests/ -q --no-cov -p no:randomly

# The npm-missing branch is an OPTIONAL skip and says what is lost, per the
# same rule dev-deps follows: a contributor working only on Python should not
# be blocked on a Node toolchain. It is a skip, never a silent pass — the
# banner names the 125 spec files that did not run, so nobody reads a green
# `make test` here as "the client is fine".
#
# The missing-node_modules branch is NOT lenient. `npm test` with no
# node_modules fails with "vitest: not found", which reads as a broken repo
# rather than an un-run install step.
test-client:
	@if [ ! -d feral-client-v2 ]; then \
	    echo "  [error] feral-client-v2 is missing from this tree."; \
	    exit 1; \
	fi
	@if ! command -v npm >/dev/null 2>&1; then \
	    echo "  [skip] feral-client-v2 vitest suite: npm not found on PATH."; \
	    echo "         LOST: the entire client suite (125 spec files) did not run."; \
	    echo "         Install Node >= 20 and run 'make dev-deps' to cover it."; \
	    exit 0; \
	fi; \
	if [ ! -d feral-client-v2/node_modules ]; then \
	    echo "  [error] feral-client-v2/node_modules is missing."; \
	    echo "          Run:  make dev-deps"; \
	    exit 1; \
	fi; \
	cd feral-client-v2 && npm test

# This target used to run pytest with `2>/dev/null || true`: stderr discarded,
# exit code forced to zero. It reported success unconditionally, including
# when every test in the repo failed, and it did not lint anything. A gate
# that cannot fail is worse than no gate, because people trust it.
#
# It now runs the exact ruff invocation CI runs, so a green `make lint` and a
# green CI mean the same thing. The ignore list is CI's, not a preference:
# keep the two in step or this stops being a preview of the gate.
lint:
	cd feral-core && $(PYTHON) -m ruff check \
		--select=E,F,W --ignore=E501,E402,F401,W291,W293 .

# ── Utilities ────────────────────────────────────────────────

doctor:
	$(FERAL) doctor

bundle-webui:
	bash scripts/build_webui.sh

clean:
	rm -rf feral-core/webui
	rm -rf feral-core/*.egg-info
	rm -rf feral-core/__pycache__
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Separate from `clean` on purpose: removing the repo-local uv means the
# next `make dev` re-downloads ~40MB, which is not what someone clearing
# build artifacts is asking for.
clean-uv:
	rm -rf $(CURDIR)/.uv

help:
	@echo ""
	@echo "  FERAL Makefile"
	@echo "  ───────────────"
	@echo ""
	@echo "  Quick start:"
	@echo "    make install       pip install + prompt for setup"
	@echo "    make setup         run the guided setup wizard"
	@echo ""
	@echo "  Development:"
	@echo "    make dev           install all deps (brain + client)"
	@echo "                       into the pinned .venv (.python-pin)"
	@echo "    make dev-python    create the pinned .venv only"
	@echo "    make dev-reset     delete .venv and rebuild it"
	@echo "    make dev-verify    print this interpreter's SQLite features"
	@echo "    make serve         start the brain server"
	@echo "    make client        start the web UI dev server"
	@echo ""
	@echo "  Testing & quality:"
	@echo "    make test          both suites (test-py + test-client)"
	@echo "    make test-py       feral-core pytest suite (shuffled; prints seed)"
	@echo "    make test-py-ci    same suite in CI's deterministic order"
	@echo "    make test-client   feral-client-v2 vitest suite only"
	@echo "    make e2e           playwright browser specs (needs make dev-deps)"
	@echo "    make lint          ruff, the exact ruleset CI gates on"
	@echo ""
	@echo "  Docker:"
	@echo "    make docker        build and start all services"
	@echo "    make docker-down   stop all services"
	@echo "    make docker-logs   tail service logs"
	@echo ""
	@echo "  Utilities:"
	@echo "    make doctor        check system health"
	@echo "    make bundle-webui  build client into feral-core/webui/"
	@echo "    make clean         remove build artifacts"
	@echo ""
