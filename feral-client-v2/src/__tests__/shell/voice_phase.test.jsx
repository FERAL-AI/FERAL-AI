/**
 * The chained voice path had no per-turn state on the desktop.
 *
 * The brain emits three different things and they answer three
 * different questions:
 *
 *   voice_status  provider health (degraded, quota exhausted, ...)
 *   voice_state   the per-turn phase: idle/listening/processing/
 *                 speaking/error, from voice/chained_pipeline.py
 *   (hook state)  is a session open at all: off/starting/active/...
 *
 * `useVoiceMode` handled the first and the third. `voice_state` fell
 * through to `default: break;` and was dropped. On the realtime path
 * RealtimeVoiceEngine drives the phase locally so nothing showed, but
 * on the chained path that frame is the ONLY source, so a whole
 * session rendered as "Listening. Speak naturally." while FERAL was
 * thinking and then speaking, and a pipeline error never reached the
 * screen at all.
 *
 * Separately, `active` mapped to the `speaking` orb mode, so an open
 * session animated as if FERAL were talking the entire time it was
 * waiting for the user, and `listening` (a mode Orb.jsx styles) was
 * unreachable from anywhere in the app.
 */
import fs from 'node:fs';
import path from 'node:path';
import { describe, it, expect } from 'vitest';

import { PHASE_MODE, PHASE_TEXT } from '../../shell/VoiceOverlay';

const ORB = path.resolve(__dirname, '../../ui/Orb.jsx');
const PIPELINE = path.resolve(
  __dirname, '../../../../feral-core/voice/chained_pipeline.py',
);
const HOOK = path.resolve(__dirname, '../../hooks/useVoiceMode.js');
const OVERLAY = path.resolve(__dirname, '../../shell/VoiceOverlay.jsx');

/** Modes Orb.jsx has a class for. */
function orbModes() {
  const src = fs.readFileSync(ORB, 'utf8');
  const block = src.slice(src.indexOf('const MODE_CLASS'), src.indexOf('};', src.indexOf('const MODE_CLASS')));
  return new Set([...block.matchAll(/^\s*([a-z]+):/gm)].map((m) => m[1]));
}

/** Values of the brain's VoiceState enum. */
function brainPhases() {
  const src = fs.readFileSync(PIPELINE, 'utf8');
  const start = src.indexOf('class VoiceState');
  const block = src.slice(start, src.indexOf('\n\n', start));
  return new Set([...block.matchAll(/=\s*"([a-z]+)"/g)].map((m) => m[1]));
}

describe('the phase map matches the brain enum', () => {
  it('reads the real pipeline, so this is not checking itself', () => {
    expect(fs.existsSync(PIPELINE)).toBe(true);
    expect(brainPhases().size).toBeGreaterThanOrEqual(5);
  });

  it('handles every phase the pipeline can emit except idle', () => {
    // `idle` deliberately has no entry: it means "no turn in progress",
    // which is exactly when the session-state mapping should win.
    const unhandled = [...brainPhases()].filter(
      (p) => p !== 'idle' && !(p in PHASE_MODE),
    );
    expect(unhandled, `phases with no orb mode: ${unhandled.join(', ')}`).toEqual([]);
  });

  it('never names an orb mode that has no class', () => {
    const modes = orbModes();
    const bogus = Object.values(PHASE_MODE).filter((m) => !modes.has(m));
    expect(bogus, `orb modes that do not exist: ${bogus.join(', ')}`).toEqual([]);
  });

  it('does not invent phases the brain never sends', () => {
    const phases = brainPhases();
    const extra = Object.keys(PHASE_MODE).filter((p) => !phases.has(p));
    expect(extra, `mapped phases the pipeline never emits: ${extra.join(', ')}`).toEqual([]);
  });

  it('gives the user words for each phase that is not an error', () => {
    for (const p of Object.keys(PHASE_MODE)) {
      if (p === 'error') continue;   // error text comes from the payload
      expect(PHASE_TEXT[p], `no status text for phase ${p}`).toBeTruthy();
    }
  });
});

describe('the hook consumes the frame', () => {
  const hook = fs.readFileSync(HOOK, 'utf8');

  it('has a case for voice_state', () => {
    // Dropping to `default` is the original bug, and it is invisible:
    // no error, no warning, just a UI that never moves.
    expect(hook).toContain("case 'voice_state'");
  });

  it('exposes the phase to whatever renders it', () => {
    expect(hook).toMatch(/return \{[\s\S]*\bphase,/);
    expect(hook).toMatch(/return \{[\s\S]*\bphaseError,/);
  });

  it('clears the phase when a session starts and when it stops', () => {
    // A phase left over from the last session would render the new one
    // as "speaking" before a word had been said.
    expect((hook.match(/setPhase\(null\)/g) || []).length).toBeGreaterThanOrEqual(2);
  });
});

describe('the overlay no longer says one thing for a whole session', () => {
  const overlay = fs.readFileSync(OVERLAY, 'utf8');

  it('reaches the listening orb mode', () => {
    expect(overlay).toContain("'listening'");
  });

  it('does not claim an idle session is speaking', () => {
    expect(overlay).not.toMatch(/voice\.state === 'active' \? 'speaking'/);
  });

  it('prefers the brain phase over the session state', () => {
    expect(overlay).toMatch(/PHASE_MODE\[voice\.phase\]/);
  });

  it('surfaces a pipeline error rather than swallowing it', () => {
    expect(overlay).toMatch(/voice\.phase === 'error'/);
    expect(overlay).toMatch(/voice\.phaseError/);
  });
});
