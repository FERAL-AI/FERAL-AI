/**
 * An empty approvals queue answered neither question a person has.
 *
 * Reported: "when I click on needs you it's empty, let's try things and
 * it should have a bit more than that, I don't know something is
 * missing there."
 *
 * The page was a title, one sentence and a Refresh button. Accurate,
 * and useless, because the two things you want to know are:
 *
 *   will anything ever stop and ask me?   -> the autonomy tier
 *   what would land here if it did?       -> the policy
 *
 * The tier matters most and was nowhere on the page. On `loose` the
 * brain never asks, so an empty queue means "nothing will EVER appear
 * here", which used to look exactly like "nothing right now". Both are
 * real endpoints (`GET /api/autonomy`, `GET /api/policy`) that this page
 * simply never called.
 */
import { describe, it, expect } from 'vitest';
import { policyLines, TIERS } from '../../pages/Approvals';

// The exact payload a running brain returns, captured from
// GET /api/policy on 127.0.0.1.
const REAL_POLICY = {
  version: '1.0',
  name: 'default',
  permissions: {
    max_tier: 'active',
    require_confirmation_above: 'active',
    auto_approve_categories: ['sensor', 'display'],
  },
  network: {
    mode: 'allowlist',
    allowed_domains: ['api.openai.com', 'api.anthropic.com', 'api.tavily.com'],
    blocked_domains: [],
  },
  filesystem: {
    read_paths: ['~/.feral/', '/tmp/feral/'],
    write_paths: ['~/.feral/skills/', '~/.feral/memory/', '/tmp/feral/'],
  },
};

describe('the tiers offered', () => {
  it('are the three the brain accepts, each with what it means', () => {
    expect(TIERS.map(([t]) => t)).toEqual(['strict', 'hybrid', 'loose']);
    for (const [, meaning] of TIERS) expect(meaning.length).toBeGreaterThan(0);
  });
});

describe('what the policy says, in sentences', () => {
  it('reads the real payload', () => {
    const lines = policyLines(REAL_POLICY);
    expect(lines.join(' ')).toContain('sensor, display');
    expect(lines.join(' ')).toContain('active');
    expect(lines.join(' ')).toContain('~/.feral/skills/');
    expect(lines.join(' ')).toContain('3 domains');
  });

  it('says nothing about a field the brain did not send', () => {
    // The alternative is a sentence asserting a default the brain may
    // not be using, which is worse than an absent line.
    expect(policyLines({})).toEqual([]);
    expect(policyLines(null)).toEqual([]);
    expect(policyLines({ permissions: {} })).toEqual([]);
  });

  it('drops empty lists rather than rendering "Runs without asking: "', () => {
    expect(policyLines({ permissions: { auto_approve_categories: [] } })).toEqual([]);
    expect(policyLines({ filesystem: { write_paths: [] } })).toEqual([]);
  });

  it('truncates a long write list instead of printing all of it', () => {
    const lines = policyLines({
      filesystem: { write_paths: ['/a', '/b', '/c', '/d', '/e'] },
    });
    expect(lines[0]).toContain('and more');
    expect(lines[0]).not.toContain('/e');
  });

  it('only calls the network an allowlist when it is one', () => {
    const open = policyLines({ network: { mode: 'open', allowed_domains: [] } });
    expect(open.join(' ')).not.toContain('allowlist');
  });

  it('survives a malformed payload without throwing', () => {
    for (const junk of [{ permissions: null }, { network: 'nope' }, { filesystem: 5 }]) {
      expect(() => policyLines(junk)).not.toThrow();
    }
  });
});
