import React, { useLayoutEffect, useMemo } from 'react';
import { AlertTriangle } from 'lucide-react';
import { useSystemHealth } from '../hooks/useSystemHealth';
import CopyButton from '../ui/CopyButton';

/**
 * RuntimeNotice: the strip that says the brain is not the build you
 * installed.
 *
 * THE FAILURE THIS EXISTS FOR
 *
 * A Python process never reloads its source. `pip install --upgrade
 * feral-ai` against a brain that is already serving succeeds, prints
 * nothing alarming, and changes nothing: the process keeps executing
 * the code it read at import. The dashboard keeps rendering. It just
 * keeps rendering the old build. Measured on a real install, a brain
 * served for two days and one hour from code that predated four
 * releases, and no surface in the product said a word.
 *
 * `feral-core/config/staleness.py` now detects it locally (frozen
 * `version.VERSION` vs a fresh `importlib.metadata` read) and
 * `GET /api/dashboard` carries the answer as `runtime`. This is the
 * half that puts it in front of the operator.
 *
 * WHY A STRIP AND NOT A TOAST OR A MODAL
 *
 * A toast expires, and the condition does not: the brain stays stale
 * until somebody restarts it, so a notice that disappears on a timer is
 * a notice that hides a real problem. A modal blocks an app that is
 * otherwise working fine. There is no dismiss control at all for the
 * same reason. The strip is chrome: the shell reserves exactly its
 * height by shifting `.v2-shell-body` down (see `--v2-chrome-top` in
 * ui.css), so unlike every fixed overlay in this client it cannot come
 * to rest on top of a control.
 *
 * WHY IT READS FROM THE SHARED STORE
 *
 * `useSystemHealth` is the single subscriber to `/api/dashboard` for
 * the whole shell. Adding a second poll for one boolean is what
 * AUDIT-r14 finding 03 was about. Subscribing here (rather than in
 * Shell) also keeps the 15s tick from re-rendering the entire shell
 * tree, since this component is a leaf.
 *
 * ADDING A SECOND KIND OF NOTICE
 *
 * `runtimeNotices()` returns a LIST, the strip renders one row per
 * entry, and the reserved height is `rows * --v2-runtime-notice-row`.
 * A second condition (for instance an update-availability check
 * reporting that a newer release exists on PyPI, which is a different
 * question needing the network) is a second branch in that function and
 * nothing else. No field for that exists on the payload today, so
 * nothing here reads one: `runtimeNotices` only answers about what
 * `staleness.py` actually sends.
 */

/** The trimmed string, or '' for anything that is not one. */
function text(value) {
  return typeof value === 'string' ? value.trim() : '';
}

/**
 * Seconds rendered the way the brain renders them: "2d 1h", "3h 20m",
 * "12m". Mirrors `config/staleness._human_uptime` so the strip and the
 * brain's own log line do not disagree about how long this has been
 * true.
 *
 * Returns '' for anything unusable, because "up NaN" is worse than no
 * uptime at all.
 *
 * A `number` and nothing else. `Number(null)` is 0, so a coercing check
 * turns a MISSING uptime into the claim "Up 0m", which is a statement
 * about the brain that nothing in the payload supports. A real 0.0
 * still reads "0m", because that one is a reading.
 */
export function humanUptime(seconds) {
  const total = seconds;
  if (typeof total !== 'number' || !Number.isFinite(total) || total < 0) return '';
  const whole = Math.floor(total);
  const days = Math.floor(whole / 86400);
  const hours = Math.floor((whole % 86400) / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

/** The one thing that makes a stale brain not stale. */
export const RESTART_COMMAND = 'feral restart';

/**
 * The notices the `runtime` block earns, newest problem first.
 *
 * `stale === true` and not merely truthy. The brain is deliberate about
 * only setting it on real evidence (an unreadable version reports
 * `stale: false` with a reason), so a payload that arrives malformed
 * should stay silent rather than tell somebody to restart on the
 * strength of a shape we did not recognise.
 *
 * @param {unknown} runtime the `runtime` block off /api/dashboard
 * @returns {{id: string, headline: string, detail: string, command: string}[]}
 */
export function runtimeNotices(runtime) {
  if (!runtime || typeof runtime !== 'object' || Array.isArray(runtime)) return [];
  const notices = [];

  if (runtime.stale === true) {
    const running = text(runtime.running_version);
    const installed = text(runtime.installed_version);
    const up = humanUptime(runtime.uptime_s);
    // The brain's own `detail` is a paragraph written for a log line
    // and is far too long for a row of chrome, so the versions are
    // re-stated here in one clause. It is still the fallback: a payload
    // that says `stale` without two usable version strings must not
    // produce a notice with nothing under the headline.
    let detail = '';
    if (running && installed) {
      detail = `Running ${running}, but ${installed} is installed.`;
      if (up) detail += ` Up ${up}.`;
    } else {
      detail = text(runtime.detail);
    }
    notices.push({
      id: 'stale-build',
      headline: 'Restart FERAL to finish updating.',
      detail,
      command: RESTART_COMMAND,
    });
  }

  return notices;
}

export default function RuntimeNotice() {
  const { data } = useSystemHealth();
  const runtime = data?.runtime;
  const notices = useMemo(() => runtimeNotices(runtime), [runtime]);
  const rows = notices.length;

  // Reserve the strip's height on the shell so the page is laid out
  // BELOW it rather than under it. The row height itself stays in the
  // stylesheet and only the count crosses over, so there is exactly one
  // place the number is written down.
  //
  // `document.querySelector` rather than a ref: this effect has to run
  // on the transition back to zero rows, and on that render the
  // component returns null and has no node to walk up from.
  useLayoutEffect(() => {
    const shell = typeof document === 'undefined'
      ? null
      : document.querySelector('.v2-shell');
    if (!shell) return undefined;
    if (rows > 0) shell.style.setProperty('--v2-runtime-notice-rows', String(rows));
    else shell.style.removeProperty('--v2-runtime-notice-rows');
    return () => shell.style.removeProperty('--v2-runtime-notice-rows');
  }, [rows]);

  // The healthy case is the common case and it renders nothing at all:
  // no strip, no empty container, no reserved pixel.
  if (rows === 0) return null;

  return (
    <div
      className="v2-runtime-notice"
      role="status"
      data-testid="runtime-notice"
      data-rows={rows}
    >
      {notices.map((notice) => (
        <div
          key={notice.id}
          className="v2-runtime-notice__row"
          data-notice={notice.id}
        >
          <AlertTriangle size={13} aria-hidden="true" className="v2-runtime-notice__icon" />
          {/* The remedy is in the first phrase, before anything that is
              allowed to truncate. An operator who reads only the bold
              words still knows what to do. */}
          <span className="v2-runtime-notice__lede">{notice.headline}</span>
          {notice.command && (
            <span className="v2-runtime-notice__do">
              Run <code className="v2-runtime-notice__cmd">{notice.command}</code> in a terminal.
            </span>
          )}
          {notice.detail && (
            <span className="v2-runtime-notice__detail">{notice.detail}</span>
          )}
          {notice.command && (
            <CopyButton
              value={notice.command}
              label={`Copy ${notice.command}`}
              copiedLabel={`Copied ${notice.command}`}
              className="v2-runtime-notice__copy"
              testId="runtime-notice-copy"
            />
          )}
        </div>
      ))}
    </div>
  );
}
