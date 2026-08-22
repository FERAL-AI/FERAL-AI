/**
 * Shared machinery for the real-brain e2e lane.
 *
 * Every other spec in this directory stubs `**\/api/**` and runs against
 * `vite preview`. That server answers index.html for ANY path with
 * status 200 and hosts no API at all, which makes a whole class of
 * defect structurally invisible:
 *
 *   - a route the brain does not actually serve (the preview server
 *     serves every route, so a 404 cannot happen),
 *   - a page whose JSON fetch receives HTML and dies on JSON.parse
 *     (the stub always returns JSON, so it cannot happen),
 *   - a `/api/*` path the client calls that no router registers
 *     (the stub answers every path, so it cannot happen),
 *   - a control that reports success while its request failed
 *     (the stub never fails).
 *
 * The specs that import this file talk to a REAL brain and stub
 * nothing. They are opt-in: `FERAL_E2E_REAL_BRAIN=1` plus a
 * `FERAL_E2E_URL` pointing at a running instance, because without a
 * brain there is nothing to assert against. See
 * `.github/workflows/v2-real-brain-e2e.yml`.
 */
import type { APIRequestContext, Page } from '@playwright/test';

/** True when the caller opted in and named a brain to talk to. */
export const REAL_BRAIN = process.env.FERAL_E2E_REAL_BRAIN === '1';

export const SKIP_REASON =
  'real-brain lane: set FERAL_E2E_REAL_BRAIN=1 and FERAL_E2E_URL=<running brain> to run';

export { readDestinations } from './destinations';
export type { Destination } from './destinations';

/** One observed HTTP exchange the page made. */
export type Exchange = {
  method: string;
  url: string;
  pathname: string;
  status: number;
  contentType: string;
  /** Playwright's classification: 'fetch'/'xhr' for a data call. */
  resourceType: string;
  fromServiceWorker: boolean;
};

/** A request the browser could not complete at all. */
export type Failure = { method: string; url: string; error: string };

export type Recorder = {
  exchanges: Exchange[];
  failures: Failure[];
  consoleErrors: string[];
  pageErrors: string[];
  /** WebSocket URLs the page opened, and any that errored. */
  sockets: string[];
  socketErrors: string[];
  /** Forget everything recorded so far; used between control clicks. */
  reset(): void;
  /** A snapshot marker so a caller can diff "what happened since". */
  mark(): number[];
  since(mark: number[]): {
    exchanges: Exchange[];
    failures: Failure[];
    consoleErrors: string[];
    pageErrors: string[];
  };
};

/**
 * Console lines this lane does not count as an app error.
 *
 * Short list, and each entry says why it is not being swept under a rug:
 *
 *  - `Failed to load resource: ... status NNN` is the browser narrating
 *    a response, not the page throwing. The recorder already holds that
 *    exact exchange with its status, and `failedRequests` asserts on it
 *    properly, including whether the page told the user. Counting it
 *    twice would report one failure as two and hide which check found
 *    it.
 *  - favicon and manifest-icon fetches are the browser's own initiative.
 *  - `Download the React DevTools` is React's dev-build banner.
 *  - The `frame-ancestors` meta notice is a REAL defect, pinned by the
 *    recorded reproduction in real_brain_controls.spec.ts rather than
 *    ignored. It is listed here only so one known defect does not mask
 *    every other finding on /apps.
 *
 * Everything else, including every uncaught exception, every unhandled
 * rejection and every WebSocket failure, is reported.
 */
const IGNORED_CONSOLE = [
  /^Failed to load resource:/i,
  /favicon/i,
  /Download the React DevTools/i,
  /\[FERAL\] SW registration failed/i,
  /'frame-ancestors' is ignored when delivered via a <meta> element/i,
];

export function record(page: Page): Recorder {
  const exchanges: Exchange[] = [];
  const failures: Failure[] = [];
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const sockets: string[] = [];
  const socketErrors: string[] = [];

  // REST is not the whole conversation. The shell opens `/v1/session`
  // and the chat composer stays disabled until that socket connects, so
  // a page whose REST calls all return 200 can still be unable to talk
  // to the brain at all. Nothing in the stubbed lane can see this:
  // `page.route` does not intercept WebSockets.
  page.on('websocket', (ws) => {
    sockets.push(ws.url());
    ws.on('socketerror', (err) => socketErrors.push(`${ws.url()}: ${err}`));
  });

  page.on('response', (res) => {
    const req = res.request();
    let pathname = '';
    try {
      pathname = new URL(res.url()).pathname;
    } catch {
      pathname = res.url();
    }
    exchanges.push({
      method: req.method(),
      url: res.url(),
      pathname,
      status: res.status(),
      contentType: (res.headers()['content-type'] || '').toLowerCase(),
      resourceType: req.resourceType(),
      fromServiceWorker: res.fromServiceWorker(),
    });
  });

  page.on('requestfailed', (req) => {
    failures.push({
      method: req.method(),
      url: req.url(),
      error: req.failure()?.errorText || 'unknown',
    });
  });

  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    if (IGNORED_CONSOLE.some((re) => re.test(text))) return;
    consoleErrors.push(text);
  });

  page.on('pageerror', (err) => {
    pageErrors.push(`${err.name}: ${err.message}`);
  });

  return {
    exchanges,
    failures,
    consoleErrors,
    pageErrors,
    sockets,
    socketErrors,
    reset() {
      exchanges.length = 0;
      failures.length = 0;
      consoleErrors.length = 0;
      pageErrors.length = 0;
      sockets.length = 0;
      socketErrors.length = 0;
    },
    mark() {
      return [exchanges.length, failures.length, consoleErrors.length, pageErrors.length];
    },
    since(m: number[]) {
      return {
        exchanges: exchanges.slice(m[0]),
        failures: failures.slice(m[1]),
        consoleErrors: consoleErrors.slice(m[2]),
        pageErrors: pageErrors.slice(m[3]),
      };
    },
  };
}

/**
 * Data calls the page made to the brain.
 *
 * Deliberately NOT `pathname.startsWith('/api/')`. The client's REST
 * surface is not all under /api: `pages/Skills.jsx:44` fetches the bare
 * path `/skills`, and `lib/api.js` has call sites for `/health` and the
 * `/internal/*` family too. A guard that only watched /api would have
 * been blind to exactly the page whose breakage prompted this lane.
 *
 * Classified by Playwright's resource type instead, so anything the
 * page fetched as data is in scope and documents, assets, images and
 * the service worker script are not.
 */
export function dataExchanges(all: Exchange[]): Exchange[] {
  return all.filter(
    (e) => (e.resourceType === 'fetch' || e.resourceType === 'xhr')
      && !e.pathname.startsWith('/assets/'),
  );
}

/** The subset under /api, for reporting. */
export function apiExchanges(all: Exchange[]): Exchange[] {
  return all.filter((e) => e.pathname.startsWith('/api/'));
}

/**
 * Anything that is not a page navigation, answered with HTML.
 *
 * This is the single highest-value assertion in the lane, because the
 * SPA catch-all mount turns every missing path into a 200. Measured on
 * the brain under test: `/api/nope` returns 404 application/json, but
 * `/nope` returns 200 text/html. So a path the client gets wrong does
 * not fail, it succeeds with the wrong body, and the caller finds out
 * by exception or not at all.
 *
 * Three shipped defects share this one shape, which is why the filter
 * is `resourceType !== 'document'` rather than anything narrower:
 *
 *   - a JSON fetch answered with index.html, which is how the Skills
 *     page shipped broken (`response.json()` throws on `<`),
 *   - a depth-2 route requesting `./assets/index-<hash>.js`, resolved
 *     against `/apps/publish`, answered with index.html as JavaScript,
 *     rendering a white screen,
 *   - an icon or asset the bundle references and does not contain,
 *     answered with index.html, so the browser decodes HTML as an image
 *     and silently shows nothing. Found this way: index.html declared
 *     `/favicon.ico`, which exists nowhere in public/ or dist/.
 *
 * A real page navigation is the one thing that SHOULD be HTML, so
 * documents (including iframe documents) are excluded.
 */
export function htmlAnsweredNonDocument(all: Exchange[]): Exchange[] {
  return all.filter(
    (e) => e.resourceType !== 'document'
      && e.contentType.includes('text/html')
      && e.status < 400,
  );
}

/**
 * Anything the brain refused or fell over on.
 *
 * 404 and 5xx only. A 4xx that is not a 404 is frequently the brain
 * correctly rejecting something (an empty form, an unauthenticated
 * probe), so it is not a defect on its own; `failedRequests` below is
 * the one that catches those, paired with "did the page say so".
 */
export function badStatuses(all: Exchange[]): Exchange[] {
  return all.filter((e) => e.status === 404 || e.status >= 500);
}

/** Every response the caller cannot have used. */
export function failedRequests(all: Exchange[]): Exchange[] {
  return all.filter((e) => e.status >= 400);
}

/**
 * The brain's registered REST paths, as matchers.
 *
 * FastAPI publishes them templated (`/api/devices/{device_id}`), so a
 * literal set membership test would report every parameterised call as
 * missing. Each path becomes a regex with `{param}` widened to one
 * segment.
 */
export async function brainRoutes(request: APIRequestContext, baseURL: string) {
  const res = await request.get(`${baseURL}/openapi.json`);
  if (!res.ok()) {
    throw new Error(`openapi.json unavailable at ${baseURL}: ${res.status()}`);
  }
  const doc = await res.json();
  // `/{full_path}` is the SPA catch-all mount. Left in, it matches every
  // single-segment path and launders a call to a route nobody wrote
  // into "registered". It is a static file mount, not an endpoint.
  const paths: string[] = Object.keys(doc.paths || {}).filter(
    (p) => p !== '/{full_path}',
  );
  const matchers = paths.map((p) => ({
    template: p,
    re: new RegExp(
      `^${p
        .replace(/[.*+?^$()|[\]\\]/g, '\\$&')
        .replace(/\{[^}]+\}/g, '[^/]+')}$`,
    ),
  }));
  return {
    paths,
    /** The template that claims `pathname`, or '' when nothing does. */
    match(pathname: string): string {
      const hit = matchers.find((m) => m.re.test(pathname));
      return hit ? hit.template : '';
    },
  };
}

/** Wait for the page to stop talking, without failing when it never does. */
export async function settle(page: Page, ms = 1500) {
  await page.waitForLoadState('networkidle', { timeout: 8000 }).catch(() => {});
  await page.waitForTimeout(ms);
}

/**
 * Controls this lane will not click, matched against a control's
 * accessible name, visible text, title and testid.
 *
 * The work order's rule is "skip anything destructive but do exercise
 * reads, refreshes, tabs, filters, modals and navigation", so this is a
 * blocklist and everything else is fair game. The lane therefore
 * mutates state and MUST be pointed at a disposable FERAL_HOME.
 *
 * `pause` and `kill` between them cover the supervisor kill switch,
 * whose button reads "Pause actions" and whose title calls itself the
 * kill switch.
 *
 * The `\b(...)\b` wrapper is load-bearing, not tidiness. Without it
 * `kill` matched "Skills", so the Dock's Skills tile was skipped as
 * destructive on every single route, and the walk reported that as a
 * clean run. A blocklist that silently over-matches is a walk that
 * silently stops walking.
 */
export const DESTRUCTIVE = new RegExp(
  `\\b(${[
    'delete', 'remove', 'revoke', 'uninstall', 'kill', 'reboot', 'restart',
    'shut ?down', 'wipe', 'purge', 'erase', 'destroy', 'reset', 'factory',
    'unpair', 'forget', 'disconnect', 'log ?out', 'sign ?out', 'prune',
    'terminate', 'abort', 'trash', 'discard', 'drop', 'pause', 'danger',
    // Outward effects that leave this machine, or that install code.
    'publish', 'install', 'deploy', 'promote', 'approve', 'deny', 'reject',
    // Leaves the app entirely and takes the walk with it.
    'download', 'export',
  ].join('|')})\\b`,
  'i',
);

export type Control = {
  /** Stable-ish identity: tag, accessible label, class. */
  sig: string;
  label: string;
  tag: string;
  index: number;
  /** Why it was skipped, or '' when it was clicked. */
  skipReason: string;
  /** Already the selected tab / current link / pressed toggle. */
  active: boolean;
};

/** Every control on the page, tagged with whether it is safe to click. */
export const CONTROL_SELECTOR = 'button, a[href], [role="button"], [role="tab"], summary';

export async function enumerateControls(page: Page): Promise<{ safe: Control[]; skipped: Control[] }> {
  const raw = await page.evaluate((sel) => {
    const nodes = [...document.querySelectorAll(sel)] as HTMLElement[];
    return nodes.map((el, index) => {
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
      return {
        index,
        tag: el.tagName.toLowerCase(),
        aria: el.getAttribute('aria-label') || '',
        title: el.getAttribute('title') || '',
        testid: el.getAttribute('data-testid') || '',
        text: text.slice(0, 80),
        href: (el as HTMLAnchorElement).href || '',
        target: el.getAttribute('target') || '',
        download: el.hasAttribute('download'),
        disabled: (el as HTMLButtonElement).disabled === true
          || el.getAttribute('aria-disabled') === 'true',
        cls: (el.className && typeof el.className === 'string' ? el.className : '').slice(0, 60),
        // Already the selected tab, the current nav item, the pressed
        // toggle. Clicking one of these legitimately changes nothing,
        // and calling that "dead" is a false accusation, not a finding.
        active: el.getAttribute('aria-selected') === 'true'
          || el.getAttribute('aria-pressed') === 'true'
          || el.getAttribute('aria-current') !== null
          || / is-active|^is-active|active$/.test(
            typeof el.className === 'string' ? el.className : '',
          ),
        visible: rect.width > 0 && rect.height > 0
          && style.visibility !== 'hidden' && style.display !== 'none',
        inHiddenSubtree: !!el.closest('[aria-hidden="true"]'),
        origin: window.location.origin,
      };
    });
  }, CONTROL_SELECTOR);

  const safe: Control[] = [];
  const skipped: Control[] = [];
  for (const r of raw) {
    const label = (r.aria || r.text || r.title || r.testid || `${r.tag}.${r.cls}`).trim();
    const haystack = `${r.aria} ${r.text} ${r.title} ${r.testid}`;
    const external = r.href
      && !r.href.startsWith(r.origin)
      && !r.href.startsWith('#');
    const destructive = DESTRUCTIVE.exec(haystack);

    let skipReason = '';
    if (!r.visible) skipReason = 'not rendered';
    else if (r.inHiddenSubtree) skipReason = 'inside an aria-hidden subtree';
    else if (r.disabled) skipReason = 'disabled';
    else if (external) skipReason = `off-site: ${r.href.slice(0, 60)}`;
    else if (r.target === '_blank') skipReason = 'opens a new tab';
    else if (r.download) skipReason = 'downloads a file';
    else if (destructive) skipReason = `destructive: matched "${destructive[0]}"`;

    const control: Control = {
      sig: `${r.tag}|${label}|${r.cls}`,
      label,
      tag: r.tag,
      index: r.index,
      skipReason,
      active: r.active,
    };
    if (skipReason) skipped.push(control);
    else safe.push(control);
  }
  return { safe, skipped };
}
