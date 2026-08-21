/**
 * The bar was reading fields that do not exist.
 *
 * `useMachineVitals` pulled `cost_today`, `spend_today`, `tokens_used`
 * and `autonomy` off `/api/dashboard`. Measured against a running
 * brain, that payload has none of them: its keys are audio_available,
 * boot, channels, demo, device_count, devices, health, is_demo_mode,
 * llm_available, memory, online_count, paired_*, session_count,
 * skills_count, somatic, subdevices_*, sync, taskflows, wake_word_*,
 * wasm_available.
 *
 * So cost read 0 and autonomy read '' on every install, and the
 * render-if-non-zero rule then hid both. The bar looked sparse because
 * it was reading nothing, not because the machine was idle. The memory
 * vital was a second misreading: the design labels its 12.4k
 * "episodes" in its own popover, and `memory.tokens` does not exist
 * either.
 */
import { describe, it, expect } from 'vitest';
import { visibleVitals } from '../../shell/SystemBar';
import { rowsFor, compact, money, AUTONOMY_TIERS } from '../../shell/VitalPopover';

const base = {
  running: 0, shells: 0, needs: 0, devices: 0, episodes: 0, skills: 0,
  cost: 0, budget: 0, budgetOn: false, costKnown: false, autonomy: '',
};

describe('which vitals the bar shows', () => {
  it('shows a cost of zero, because zero is a reading', () => {
    // "$0.00 spent today" and "the brain never told us" are different
    // facts and used to render identically: as nothing at all.
    const shown = visibleVitals({ ...base, costKnown: true, cost: 0 });
    expect(shown.map((v) => v.k)).toContain('cost');
    expect(shown.find((v) => v.k === 'cost').label).toBe('$0.00');
  });

  it('hides cost when the brain reported no budget at all', () => {
    expect(visibleVitals(base).map((v) => v.k)).not.toContain('cost');
  });

  it('shows autonomy whenever the brain names a tier', () => {
    const shown = visibleVitals({ ...base, autonomy: 'loose' });
    const tier = shown.find((v) => v.k === 'autonomy');
    expect(tier.label).toBe('loose');
  });

  it('counts episodes for the memory vital, not tokens', () => {
    const shown = visibleVitals({ ...base, episodes: 12410 });
    expect(shown.find((v) => v.k === 'mem').label).toBe('12.4k');
  });

  it('keeps the machine vitals present even at zero', () => {
    // Running, needs and devices are the three the operator scans for,
    // so they hold their place rather than appearing and disappearing.
    const ks = visibleVitals(base).map((v) => v.k);
    expect(ks).toEqual(['brain', 'jobs', 'needs', 'dev']);
  });

  it('only animates the sparkline while something runs', () => {
    expect(visibleVitals(base)[0].live).toBe(false);
    expect(visibleVitals({ ...base, running: 2 })[0].live).toBe(true);
  });

  it('badges needs only when something is waiting', () => {
    expect(visibleVitals(base).find((v) => v.k === 'needs').count).toBe('');
    expect(visibleVitals({ ...base, needs: 2 }).find((v) => v.k === 'needs').count).toBe('2');
  });
});

describe('what a popover puts in front of you', () => {
  it('offers kill only when the brain names a route for it', () => {
    const rows = rowsFor('jobs', {
      items: [
        { id: 'a', name: 'npm run build', status: 'running', cancellable_via: 'POST /api/jobs/a/cancel' },
        { id: 'b', name: 'orphan', status: 'running', cancellable_via: '' },
      ],
    }, base);
    expect(rows[0].verb).toBe('kill');
    // A button that 404s is worse than no button.
    expect(rows[1].verb).toBe('');
  });

  it('ignores jobs that are not running', () => {
    const rows = rowsFor('jobs', {
      items: [{ id: 'a', name: 'done', status: 'completed' }],
    }, base);
    expect(rows).toEqual([]);
  });

  it('builds an approve verb that targets the real endpoint', () => {
    const rows = rowsFor('needs', {
      approvals: [{ request_id: 'r1', tool_name: 'write_file', args: { path: '~/x' } }],
    }, base);
    expect(rows[0].verb).toBe('approve');
    expect(rows[0].act).toBe('POST /api/approvals/r1/approve');
    expect(rows[0].sub).toBe('~/x');
  });

  it('says so when the budget is unknowable rather than showing $0.00', () => {
    const rows = rowsFor('cost', {}, { ...base, costKnown: false });
    expect(rows[0].title).toBe('Not available');
  });

  it('distinguishes a real zero from a missing cap', () => {
    const capped = rowsFor('cost', {}, { ...base, costKnown: true, budgetOn: true, budget: 10, cost: 1.84 });
    expect(capped[0].value).toBe('$1.84');
    expect(capped[1].title).toBe('Daily cap');

    const uncapped = rowsFor('cost', {}, { ...base, costKnown: true, budgetOn: false });
    expect(uncapped[1].title).toBe('No daily cap');
  });

  it('reads memory counts off the dashboard payload shape', () => {
    const rows = rowsFor('mem', { memory: { episodes: 3, notes: 4, knowledge_triples: 5, embedded_chunks: 6 } }, base);
    expect(rows.map((r) => r.value)).toEqual(['3', '4', '5', '6']);
  });

  it('offers the three autonomy tiers with what each one means', () => {
    expect(AUTONOMY_TIERS.map(([t]) => t)).toEqual(['strict', 'hybrid', 'loose']);
    expect(AUTONOMY_TIERS.every(([, meaning]) => meaning.length > 0)).toBe(true);
  });

  it('survives a payload with nothing in it', () => {
    for (const k of ['jobs', 'needs', 'dev', 'mem', 'brain', 'cost']) {
      expect(Array.isArray(rowsFor(k, {}, base))).toBe(true);
    }
    expect(rowsFor('jobs', null, base)).toEqual([]);
  });
});

describe('formatting', () => {
  it('compacts large counts the way the design renders them', () => {
    expect(compact(12410)).toBe('12.4k');
    expect(compact(999)).toBe('999');
    expect(compact(1_500_000)).toBe('1.5M');
    expect(compact(0)).toBe('0');
  });

  it('renders cost with both decimals', () => {
    expect(money(1.8)).toBe('$1.80');
    expect(money(0)).toBe('$0.00');
  });
});

describe('rail row glyphs', () => {
  it('tells a shell command from a sub-agent', async () => {
    const { iconForJob } = await import('../../shell/WorkRail');
    const shell = iconForJob('background_bash');
    const agent = iconForJob('subagent');
    const other = iconForJob('');
    // The rail mixes these in one list, so the glyph is the only thing
    // that separates them at a glance.
    expect(shell).not.toBe(agent);
    expect(other).not.toBe(shell);
    // lucide icons are forwardRef objects, not plain functions.
    expect(['function', 'object']).toContain(typeof shell);
    expect(shell).toBeTruthy();
  });
});
