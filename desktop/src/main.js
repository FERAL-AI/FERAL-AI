import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';

const appEl = document.getElementById('app');
const CONFIG_KEY = 'feral_desktop_config';

// How long the splash is allowed to claim the brain is "initializing" before
// it has to admit it is not coming up. `start_brain` returning Ok only means
// a process was spawned; the brain can still die during startup (the classic
// case is an interpreter whose SQLite has no FTS5, which aborts in
// MemoryStore construction). Past this point the shell reports what it
// actually resolved instead of animating dots forever.
const BRAIN_WAIT_MS = 30_000;
const BRAIN_POLL_MS = 2_000;

function loadConfig() {
  try {
    return JSON.parse(localStorage.getItem(CONFIG_KEY)) || {};
  } catch { return {}; }
}

function saveConfig(cfg) {
  localStorage.setItem(CONFIG_KEY, JSON.stringify(cfg));
}

// Earlier builds stored a `brainUrl` and an `apiKey` collected by a setup
// form that nothing ever read (see showWelcome below). Those keys are dead,
// and an unused API key sitting in localStorage is a liability rather than a
// leftover, so an existing config is rewritten without them on first boot.
function pruneLegacyConfig(cfg) {
  if (!('brainUrl' in cfg) && !('apiKey' in cfg)) return cfg;
  const { brainUrl: _brainUrl, apiKey: _apiKey, ...rest } = cfg;
  saveConfig(rest);
  return rest;
}

const sleep = (ms) => new Promise((resolve) => { window.setTimeout(resolve, ms); });

// ---------------------------------------------------------------------------
// SVG assets (inline so no external file is needed)
// ---------------------------------------------------------------------------

// Colours are read from the design tokens via CSS custom properties. They are
// set with style="..." rather than as stop-color/fill presentation attributes,
// because var() is only resolved in CSS declarations, not in SVG attributes.
// Tokens come from src/tokens.css, which index.html links.
//
// The three gradients are kept as gradients. The v2 system is one accent plus
// neutrals, so each pair now runs accent-to-neutral instead of the old
// indigo-to-violet, which preserves the designed depth without a second hue.
const BRAIN_SVG = `
<svg viewBox="0 0 88 88" fill="none" xmlns="http://www.w3.org/2000/svg">
  <!-- Neural brain icon with sparkle accents -->
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="88" y2="88" gradientUnits="userSpaceOnUse">
      <stop offset="0%" style="stop-color: var(--v2-accent); stop-opacity: .15"/>
      <stop offset="100%" style="stop-color: var(--v2-text-primary); stop-opacity: .08"/>
    </linearGradient>
    <linearGradient id="stroke" x1="20" y1="18" x2="68" y2="72" gradientUnits="userSpaceOnUse">
      <stop offset="0%" style="stop-color: var(--v2-accent)"/>
      <stop offset="100%" style="stop-color: var(--v2-text-primary)"/>
    </linearGradient>
    <linearGradient id="sparkle" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" style="stop-color: var(--v2-text-primary)"/>
      <stop offset="100%" style="stop-color: var(--v2-accent)"/>
    </linearGradient>
  </defs>
  <circle cx="44" cy="44" r="40" fill="url(#bg)"/>
  <!-- Left hemisphere -->
  <path d="M44 24c-8 0-14 3-17 8s-4 11-2 17c1.5 4.5 5 8.5 10 11.5 3 1.8 6 3 9 3.5"
        stroke="url(#stroke)" stroke-width="2.2" stroke-linecap="round" fill="none"/>
  <!-- Right hemisphere -->
  <path d="M44 24c8 0 14 3 17 8s4 11 2 17c-1.5 4.5-5 8.5-10 11.5-3 1.8-6 3-9 3.5"
        stroke="url(#stroke)" stroke-width="2.2" stroke-linecap="round" fill="none"/>
  <!-- Central fissure -->
  <line x1="44" y1="24" x2="44" y2="64" stroke="url(#stroke)" stroke-width="1.5" stroke-dasharray="3 3" opacity=".5"/>
  <!-- Neural connections -->
  <circle cx="36" cy="36" r="2" style="fill: var(--v2-accent)" opacity=".8"/>
  <circle cx="52" cy="36" r="2" style="fill: var(--v2-accent)" opacity=".8"/>
  <circle cx="44" cy="44" r="2.5" style="fill: var(--v2-text-primary)"/>
  <circle cx="36" cy="52" r="2" style="fill: var(--v2-accent)" opacity=".8"/>
  <circle cx="52" cy="52" r="2" style="fill: var(--v2-accent)" opacity=".8"/>
  <line x1="36" y1="36" x2="44" y2="44" style="stroke: var(--v2-accent)" stroke-width="1" opacity=".4"/>
  <line x1="52" y1="36" x2="44" y2="44" style="stroke: var(--v2-accent)" stroke-width="1" opacity=".4"/>
  <line x1="36" y1="52" x2="44" y2="44" style="stroke: var(--v2-accent)" stroke-width="1" opacity=".4"/>
  <line x1="52" y1="52" x2="44" y2="44" style="stroke: var(--v2-accent)" stroke-width="1" opacity=".4"/>
  <!-- Sparkle top-right -->
  <g transform="translate(64,18)" opacity=".9">
    <path d="M4 0L5 3.5 8 4 5 5 4 8 3 5 0 4 3 3.5Z" fill="url(#sparkle)"/>
  </g>
  <!-- Sparkle bottom-left -->
  <g transform="translate(14,62)" opacity=".6">
    <path d="M3 0L3.8 2.5 6 3 3.8 3.5 3 6 2.2 3.5 0 3 2.2 2.5Z" fill="url(#sparkle)"/>
  </g>
</svg>`;

const ERROR_SVG = `
<svg class="error-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="12" cy="12" r="10"/>
  <line x1="12" y1="8" x2="12" y2="12"/>
  <line x1="12" y1="16" x2="12.01" y2="16"/>
</svg>`;

// ---------------------------------------------------------------------------
// UI states
// ---------------------------------------------------------------------------

function showStarting(url) {
  appEl.innerHTML = `
    <div class="splash">
      <div class="splash-logo">
        <div class="glow-ring"></div>
        <div class="glow-ring"></div>
        <div class="glow-ring"></div>
        <img src="/icons/128x128.png" alt="FERAL" style="width: 96px; height: 96px; border-radius: 16px; box-shadow: 0 4px 24px var(--glow);" />
      </div>
      <div class="splash-title">FERAL</div>
      <div class="splash-subtitle">One brain. Every device.</div>
      <div class="splash-dots"><span></span><span></span><span></span></div>
      <div class="splash-status" id="splash-status" aria-live="polite"></div>
    </div>
  `;
  setSplashStatus(url, 0);
}

// The dots animate on a fixed timer and measure nothing. The line below is
// the part that is actually true: which URL is being probed (as resolved by
// get_brain_url, not a hardcoded guess) and how much of the bounded wait has
// been spent. textContent, because `url` comes from an environment variable.
function setSplashStatus(url, waitedMs) {
  const el = document.getElementById('splash-status');
  if (!el) return;
  const where = url ? `on ${url}` : '(brain URL unresolved)';
  el.textContent = waitedMs < 1000
    ? `Initializing brain ${where} …`
    : `Initializing brain ${where} … waiting ${Math.round(waitedMs / 1000)}s of ${BRAIN_WAIT_MS / 1000}s`;
}

function showBrain(url) {
  // Built as a node rather than interpolated into innerHTML: `url` comes
  // from FERAL_PUBLIC_BASE_URL / FERAL_BRAIN_URL, and the page's CSP allows
  // unsafe-inline.
  appEl.replaceChildren();
  const frame = document.createElement('iframe');
  frame.src = url;
  frame.title = 'FERAL';
  frame.className = 'frame';
  frame.setAttribute('allow', 'clipboard-read; clipboard-write');
  appEl.appendChild(frame);
}

// First run. This used to be a two-field form ("Brain URL", "API Key") whose
// values were written to localStorage and read back only to re-fill the same
// form. Nothing else consumed either one, and nothing could have:
// `start_brain`, `check_brain_health` and `get_brain_url` in src-tauri take no
// parameters, the brain URL comes solely from FERAL_PUBLIC_BASE_URL /
// FERAL_BRAIN_URL read in the Rust process, no Tauri command anywhere accepts
// a key, and the app's CSP allows framing and connecting to localhost and
// 127.0.0.1 only. The desktop shell spawns and owns a local brain, so the
// fields are gone and the screen states what is actually true, including the
// URL that was really resolved and the environment variables that really
// change it.
function showWelcome(url, onContinue) {
  appEl.innerHTML = `
    <div class="splash" style="justify-content: center;">
      <div class="splash-logo">
        <div class="glow-ring"></div>
        <div class="glow-ring"></div>
        <img src="/icons/128x128.png" alt="FERAL" style="width:80px;height:80px;border-radius:14px;box-shadow:0 4px 24px var(--glow);" />
      </div>
      <div class="splash-title" style="margin-bottom:0.3rem;">Welcome to FERAL</div>
      <div class="splash-subtitle" style="margin-bottom:1.25rem;">
        This app runs its own brain on this machine, using the Python runtime
        and the copy of <code>feral-core</code> bundled inside it.
      </div>
      <div class="detail-block" id="welcome-detail"></div>
      <div class="hint">
        There is nothing to configure here. To point at a different brain, set
        <code>FERAL_BRAIN_URL</code> before launching; <code>FERAL_CORE_DIR</code>
        and <code>FERAL_PYTHON</code> select the brain source and the interpreter
        (which must have SQLite FTS5).
      </div>
      <button class="btn" type="button" id="setup-go" style="width:100%;max-width:380px;">Start FERAL</button>
    </div>
  `;
  document.getElementById('welcome-detail').textContent =
    `brain url: ${url || 'UNRESOLVED'}`;

  document.getElementById('setup-go').onclick = () => {
    saveConfig({ setupDone: true });
    onContinue();
  };
}

function showError(title, detail) {
  appEl.innerHTML = `
    <div class="error-wrap">
      ${ERROR_SVG}
      <div class="error-title" id="error-title"></div>
      <pre class="detail-block" id="error-detail"></pre>
      <div class="error-hint">
        The app normally uses the copy of the brain and the Python runtime
        bundled inside it. Set <code>FERAL_CORE_DIR</code> to point at a
        different <code>feral-core</code>, or <code>FERAL_PYTHON</code> to
        point at a specific interpreter (it must have SQLite FTS5).
      </div>
      <button class="btn" type="button" id="retry">Retry</button>
    </div>
  `;
  // textContent on both: `title` carries a Tauri error string and `detail`
  // carries filesystem paths and a health-probe message, none of it escaped.
  document.getElementById('error-title').textContent = title;
  const detailEl = document.getElementById('error-detail');
  if (detail) {
    detailEl.textContent = detail;
  } else {
    detailEl.style.display = 'none';
  }
  // Re-runs the whole boot, so Retry actually re-spawns the brain rather
  // than only repainting the screen.
  document.getElementById('retry').onclick = () => { void boot(); };
}

// What the app resolved, for the error screen. `brain_runtime_info` reports
// three lines: the feral-core directory, the interpreter (or "UNRESOLVED:"
// with the reason every candidate was rejected, e.g. missing FTS5), and the
// brain URL. See src-tauri/src/main.rs.
async function collectDiagnostics(firstLine) {
  const lines = [];
  if (firstLine) lines.push(firstLine);
  try {
    lines.push(String(await invoke('brain_runtime_info')).trim());
  } catch (e) {
    lines.push(`brain_runtime_info failed: ${e}`);
  }
  return lines.join('\n');
}

// ---------------------------------------------------------------------------
// Health polling
// ---------------------------------------------------------------------------

function isHealthyStatus(status) {
  return typeof status === 'string' && /^HTTP 2\d\d\b/.test(status);
}

async function probeBrain() {
  try {
    const status = String(await invoke('check_brain_health'));
    return { healthy: isHealthyStatus(status), status };
  } catch (e) {
    return { healthy: false, status: `health check failed: ${e}` };
  }
}

// Polls until the brain is healthy or BRAIN_WAIT_MS has elapsed, whichever
// comes first, and reports which one happened. The previous version scheduled
// a setInterval with no attempt limit and no failure branch, and returned to
// its caller immediately, so a brain that never came up left the splash
// animating for as long as the window stayed open.
async function waitForBrain(url) {
  const started = Date.now();
  let last = await probeBrain();
  while (!last.healthy) {
    const waited = Date.now() - started;
    if (waited >= BRAIN_WAIT_MS) return { ok: false, status: last.status };
    setSplashStatus(url, waited);
    await sleep(Math.min(BRAIN_POLL_MS, BRAIN_WAIT_MS - waited));
    last = await probeBrain();
  }
  return { ok: true };
}

// get_brain_url is infallible on the Rust side (it returns String), so an
// empty answer here means the bridge itself failed, which is reported rather
// than papered over with a hardcoded localhost:9090.
async function resolveBrainUrl() {
  try {
    const url = await invoke('get_brain_url');
    return typeof url === 'string' ? url.trim() : '';
  } catch (e) {
    console.error('[FERAL] get_brain_url failed', e);
    return '';
  }
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function boot() {
  const cfg = pruneLegacyConfig(loadConfig());
  const url = await resolveBrainUrl();

  if (!cfg.setupDone) {
    showWelcome(url, () => { void boot(); });
    return;
  }

  showStarting(url);

  if (!url) {
    showError(
      'Could not determine the brain URL.',
      await collectDiagnostics('get_brain_url returned nothing.'),
    );
    return;
  }

  try {
    await invoke('start_brain');
  } catch (e) {
    showError('Could not start the brain.', await collectDiagnostics(`start_brain failed: ${e}`));
    return;
  }

  // start_brain resolving only means a process was spawned with a pid. It
  // says nothing about whether that process survived its own startup.
  const result = await waitForBrain(url);
  if (!result.ok) {
    showError(
      `The brain did not become reachable within ${BRAIN_WAIT_MS / 1000} seconds.`,
      await collectDiagnostics(`last health check: ${result.status}`),
    );
    return;
  }

  showBrain(url);
}

void (async () => {
  await listen('voice-activation', () => {
    window.dispatchEvent(new CustomEvent('feral-voice-activation'));
    console.log('[FERAL] Voice shortcut (Cmd/Ctrl+Shift+T)');
  });
  await boot();
})();
