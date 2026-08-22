/**
 * Two pages read GET /api/channels and both got it wrong, differently.
 *
 * The payload is an envelope that ALSO spreads its per-channel rows to
 * the top level:
 *
 *     {active_channels: [...], channel_count: N, details: {...}, ...rows}
 *
 * Home ended `|| c.value || {}`, so on a brain with nothing configured
 * it walked the envelope and rendered `Active_channels off`,
 * `Channel_count off` and `Details off` as channels. That is what the
 * user photographed.
 *
 * Settings read `stats.status_by_channel || stats.channels`. NEITHER
 * key exists on this payload, so its channel panel listed nothing at
 * all, and `stats.active` does not exist either: the brain reports
 * `active_channels` as a LIST and `channel_count` as a number.
 *
 * One implementation now, in lib, because the same payload being
 * misread in two different ways is what a shared reader is for.
 */
import { describe, it, expect } from 'vitest';
import { channelMap, activeChannelCount } from '../../lib/channels';

// Captured verbatim from a running brain with nothing configured.
const FRESH = { active_channels: [], channel_count: 0, details: {} };

// The same endpoint with one channel connected. Note the row appears
// BOTH under `details` and spread at the top level.
const CONFIGURED = {
  active_channels: ['telegram'],
  channel_count: 1,
  details: { telegram: { connected: true, enabled: true } },
  telegram: { connected: true, enabled: true },
};

describe('channelMap', () => {
  it('renders nothing when nothing is configured', () => {
    expect(channelMap(FRESH)).toEqual({});
  });

  it('never treats the envelope fields as channels', () => {
    const out = channelMap(CONFIGURED);
    expect(Object.keys(out)).toEqual(['telegram']);
    for (const envelopeKey of ['active_channels', 'channel_count', 'details']) {
      expect(out).not.toHaveProperty(envelopeKey);
    }
  });

  it('prefers status_by_channel when a brain sends that shape', () => {
    expect(channelMap({ status_by_channel: { slack: { connected: false } } }))
      .toEqual({ slack: { connected: false } });
  });

  it('falls back to the flattened shape without the envelope', () => {
    const out = channelMap({
      active_channels: ['slack'], channel_count: 1,
      slack: { connected: true },
    });
    expect(Object.keys(out)).toEqual(['slack']);
  });

  it('does not mistake a list for the channel map', () => {
    // `details` arriving as a list would otherwise be returned whole and
    // then walked by index.
    expect(channelMap({ details: ['telegram'] })).toEqual({});
  });

  it('survives a missing or malformed payload', () => {
    for (const junk of [undefined, null, {}, { details: null }, { details: 5 }]) {
      expect(() => channelMap(junk)).not.toThrow();
      expect(channelMap(junk)).toEqual({});
    }
  });
});

describe('activeChannelCount', () => {
  it('counts the list the brain actually reports', () => {
    expect(activeChannelCount(CONFIGURED)).toBe(1);
    expect(activeChannelCount(FRESH)).toBe(0);
  });

  it('does not fall back to however many rows rendered', () => {
    // Settings used `stats.active ?? entries.length`. `stats.active`
    // does not exist, so it silently reported the render count, which
    // is a different question from "how many are connected".
    expect(activeChannelCount({ details: { a: {}, b: {}, c: {} } })).toBe(0);
  });

  it('survives junk', () => {
    for (const junk of [undefined, null, {}, { active_channels: 'telegram' }]) {
      expect(activeChannelCount(junk)).toBe(0);
    }
  });
});
