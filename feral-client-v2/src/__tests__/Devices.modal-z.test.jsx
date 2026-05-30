/**
 * modal stacking-context regression test.
 *
 * The user-facing bug: clicking "+ Pair new device" on /devices added
 * a row to the historical list but the modal never became visible.
 * Root cause: the modal rendered
 * inline inside .v2-shell-main, which itself has a positive z-index,
 * trapping the modal below the dock + menubar regardless of its own
 * z-index value.
 *
 * Two invariants from the fix that this test pins down:
 *
 * 1. Modal must mount via React Portal onto document.body so it
 * escapes the shell-main stacking context.
 * 2. The named stacking constants in styles/_z.css must order the
 * modal strictly above the page base layer (.v2-shell-main).
 *
 * The first is testable in jsdom by checking node parentage. The
 * second is a static CSS assertion — jsdom's getComputedStyle does
 * not resolve var(...) tokens, so we read the file content directly
 * and parse the numeric constants.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import Modal from '../ui/Modal';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const STYLES_DIR = path.resolve(__dirname, '../styles');

function readZConstants() {
  const css = fs.readFileSync(path.join(STYLES_DIR, '_z.css'), 'utf8');
  const constants = {};
  for (const line of css.split('\n')) {
    const m = line.match(/--(z-[a-z]+):\s*(\d+)/);
    if (m) constants[m[1]] = Number(m[2]);
  }
  return constants;
}

function readPagesCssRule(selector) {
  const css = fs.readFileSync(path.join(STYLES_DIR, 'pages.css'), 'utf8');
 // crude block extractor: find `selector { ... }` and return body text.
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(`${escaped}\\s*{([^}]*)}`, 'g');
  const blocks = [];
  let m;
  while ((m = re.exec(css)) !== null) blocks.push(m[1]);
  return blocks;
}

describe('Modal stacking-context (W4 §A.2)', () => {
  it('exposes named stacking constants in styles/_z.css', () => {
    const z = readZConstants();
    // Spec from §D deliverable 2.
    expect(z['z-base']).toBe(1);
    expect(z['z-dock']).toBe(50);
    expect(z['z-orb']).toBe(60);
    expect(z['z-overlay']).toBe(90);
    expect(z['z-modal']).toBe(100);
    expect(z['z-toast']).toBe(110);
  });

  it('places --z-modal strictly above the shell base layer', () => {
    const z = readZConstants();
 // The shell main background layer (.v2-shell-main, ui.css:80) sits
 // at z-base. Anything modal must outrank it so a portal-mounted
 // backdrop wins the cascade against page content.
    expect(z['z-modal']).toBeGreaterThan(z['z-base']);
 // …and the dock / menubar (both z-dock = 50). Without this the
 // user sees their dock paint on top of the open dialog.
    expect(z['z-modal']).toBeGreaterThan(z['z-dock']);
 // Toast layer must outrank modal so async notifications can land
 // on top of an open dialog. (Symmetry check — failing this means
 // a future refactor swapped the rungs.)
    expect(z['z-toast']).toBeGreaterThan(z['z-modal']);
  });

  it('binds .v2-modal-backdrop in pages.css to the named --z-modal token', () => {
    // The legacy literal lives in ui.css. The fix re-declares the
 // selector in pages.css (loaded after ui.css) so the cascade
 // resolves to the named constant. Drift here means ui.css's
 // numeric literal would silently win again.
    const blocks = readPagesCssRule('.v2-modal-backdrop');
    expect(blocks.length).toBeGreaterThan(0);
    const usesToken = blocks.some((b) => /z-index:\s*var\(--z-modal\)/.test(b));
    expect(usesToken).toBe(true);
  });

  it('mounts the modal via portal onto document.body, NOT inside the rendered container', () => {
    const { container } = render(
      <Modal open onClose={() => {}} title="Pair a device">
        <span data-testid="modal-content">QR placeholder</span>
      </Modal>,
    );

 // The render container is intentionally empty — the modal is in
 // document.body, escaping any caller-supplied stacking context
 // (in production: .v2-shell-main).
    expect(container.firstChild).toBeNull();

    const backdrop = document.querySelector('[data-testid="v2-modal-backdrop"]');
    expect(backdrop).not.toBeNull();
 // Direct child of body — no .v2-shell-main ancestor swallows it.
    expect(backdrop.parentElement).toBe(document.body);

 // Body should NOT contain a .v2-shell-main wrapping our backdrop.
    const wrappingShell = backdrop.closest('.v2-shell-main');
    expect(wrappingShell).toBeNull();
  });

  it('renders the dialog with the v2-modal-card class so the named z-index applies', () => {
    render(
      <Modal open onClose={() => {}} title="Pair a device">
        body
      </Modal>,
    );
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
 // pages.css pairs .v2-modal-card with z-index: calc(var(--z-modal) + 1)
 // so the inner Glass card outranks its own backdrop predictably.
    expect(dialog.className).toContain('v2-modal-card');
  });
});
