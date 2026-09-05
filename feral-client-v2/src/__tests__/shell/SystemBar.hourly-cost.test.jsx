/**
 * The cost vital said $0.00 while chat was being refused for cost.
 *
 * The brain answered `/api/dashboard` with `LLMProvider._budget_snapshot()`,
 * which resolves `llm.daily_spend_usd`, a settings key whose only producer is
 * a static 0.0 default. It is not connected to the ledger. Meanwhile the
 * enforcer, `CostBudget`, held $9.99 against a $10 hourly chat cap and was
 * turning turns away.
 *
 * The brain now sends `hour_spend_usd` and `hour_cap_usd` off that ledger.
 * The hour is the window that matters, because a chat cap is enforced per UTC
 * clock hour, so the bar shows it whenever it is present and a cap is set.
 * An older brain sends neither field and must keep the daily reading rather
 * than rendering "$0.00 / $0.00 this hour".
 */
import { describe, it, expect } from 'vitest';
import { visibleVitals } from '../../shell/SystemBar';
import { rowsFor } from '../../shell/VitalPopover';

const base = {
  running: 0, shells: 0, needs: 0, devices: 0, episodes: 0, skills: 0,
  cost: 0, budget: 0, budgetOn: false, costKnown: false, autonomy: '',
  hourCost: 0, hourCap: 0, hourKnown: false,
};

const capped = {
  ...base,
  costKnown: true,
  budgetOn: true,
  cost: 24.5,
  budget: 0,
  hourKnown: true,
  hourCost: 9.992715,
  hourCap: 10,
};

describe('the cost vital against the hourly cap', () => {
  it('shows spend over the cap for the hour that is being enforced', () => {
    const vital = visibleVitals(capped).find((v) => v.k === 'cost');
    expect(vital.title).toBe('This hour');
    expect(vital.label).toBe('$9.99 / $10.00');
    expect(vital.aria).toBe('$9.99 of $10.00 spent this hour');
  });

  it('keeps the daily reading when the brain reports no hourly figures', () => {
    const older = { ...base, costKnown: true, cost: 1.84 };
    const vital = visibleVitals(older).find((v) => v.k === 'cost');
    expect(vital.title).toBe('Today');
    expect(vital.label).toBe('$1.84');
  });

  it('does not render a cap that is not set', () => {
    const uncapped = { ...base, costKnown: true, cost: 1.84, hourKnown: true, hourCost: 0.4, hourCap: 0 };
    const vital = visibleVitals(uncapped).find((v) => v.k === 'cost');
    expect(vital.title).toBe('Today');
  });
});

describe('the cost popover', () => {
  it('leads with the hour, then the day, then the daily cap', () => {
    const rows = rowsFor('cost', {}, capped);
    expect(rows.map((r) => r.id)).toEqual(['hr', 'sp', 'cap']);
    expect(rows[0].value).toBe('$9.99 / $10.00');
    expect(rows[1].value).toBe('$24.50');
    expect(rows[2].title).toBe('No daily cap');
  });

  it('reports the hour with no cap as a plain figure', () => {
    const rows = rowsFor('cost', {}, { ...capped, hourCap: 0 });
    expect(rows[0].sub).toBe('chat, no hourly cap set');
    expect(rows[0].value).toBe('$9.99');
  });

  it('omits the hour row entirely for a brain that does not send it', () => {
    const rows = rowsFor('cost', {}, { ...base, costKnown: true, cost: 1.84, budget: 10 });
    expect(rows.map((r) => r.id)).toEqual(['sp', 'cap']);
    expect(rows[1].title).toBe('Daily cap');
  });

  it('still says so when the brain reported no budget at all', () => {
    expect(rowsFor('cost', {}, base)[0].title).toBe('Not available');
  });
});
