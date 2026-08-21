/**
 * Every kind /api/jobs can emit needs a human label.
 *
 * The aggregator's `aggregators` dict in feral-core/api/routes/jobs.py is
 * the authoritative list of kinds, and the label map here is a second
 * copy of it in another language. Adding a source on the brain side is
 * how "background_bash" ended up rendered as a chip, so this reads the
 * Python and fails if the two disagree.
 */
import fs from 'node:fs';
import path from 'node:path';
import { describe, it, expect } from 'vitest';
import { JOB_KIND_LABELS, jobKindLabel } from '../../pages/Home';

const JOBS_PY = path.resolve(
  __dirname, '../../../../feral-core/api/routes/jobs.py',
);

/** Kind strings from the `aggregators = { ... }` dict in the route. */
function brainKinds() {
  const src = fs.readFileSync(JOBS_PY, 'utf8');
  const start = src.indexOf('aggregators = {');
  const block = src.slice(start, src.indexOf('}', start));
  return [...block.matchAll(/"([a-z_]+)":\s*_/g)].map((m) => m[1]);
}

describe('job kind labels', () => {
  it('reads the brain route, so this test is not just checking itself', () => {
    expect(fs.existsSync(JOBS_PY)).toBe(true);
    expect(brainKinds().length).toBeGreaterThanOrEqual(5);
  });

  it('has a label for every kind the aggregator can return', () => {
    const missing = brainKinds().filter((k) => !(k in JOB_KIND_LABELS));
    expect(missing, `job kinds with no human label: ${missing.join(', ')}`).toEqual([]);
  });

  it('does not label kinds the brain no longer emits', () => {
    const kinds = new Set(brainKinds());
    const stale = Object.keys(JOB_KIND_LABELS).filter((k) => !kinds.has(k));
    expect(stale, `labels for kinds that no longer exist: ${stale.join(', ')}`).toEqual([]);
  });

  it('never renders a raw internal name for a known kind', () => {
    for (const k of brainKinds()) {
      expect(jobKindLabel(k)).not.toBe(k);
    }
  });

  it('falls back to the raw kind rather than hiding an unknown source', () => {
    expect(jobKindLabel('something_new')).toBe('something_new');
    expect(jobKindLabel('')).toBe('Job');
    expect(jobKindLabel(undefined)).toBe('Job');
  });
});
