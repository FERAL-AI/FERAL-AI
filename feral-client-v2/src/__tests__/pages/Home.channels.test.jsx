/**
 * The Channels card rendered the response envelope as channels.
 *
 * GET /api/channels answers an envelope that also spreads the
 * per-channel rows to the top level. The old expression ended
 * `|| c.value || {}`, so with neither `status_by_channel` nor
 * `channels` present it fell through to the envelope itself and
 * `Object.entries` walked its fields. Observed on a running brain with
 * nothing configured: three rows reading `Active_channels off`,
 * `Channel_count off`, `Details off`, where the empty state belonged.
 */
import { describe, it, expect } from 'vitest';
import { channelMap } from '../../pages/Home';

describe('channelMap', () => {
  it('returns nothing to render when no channels are configured', () => {
    // The exact payload a fresh brain answers.
    expect(channelMap({ active_channels: [], channel_count: 0, details: {} })).toEqual({});
  });

  it('never treats the envelope fields as channels', () => {
    const out = channelMap({ active_channels: ['telegram'], channel_count: 1, details: {
      telegram: { connected: true, enabled: true },
    } });
    expect(Object.keys(out)).toEqual(['telegram']);
    expect(out).not.toHaveProperty('channel_count');
    expect(out).not.toHaveProperty('active_channels');
    expect(out).not.toHaveProperty('details');
  });

  it('prefers status_by_channel when a brain sends that shape', () => {
    expect(channelMap({ status_by_channel: { slack: { connected: false } } }))
      .toEqual({ slack: { connected: false } });
  });

  it('falls back to the flattened shape, still without the envelope', () => {
    const out = channelMap({
      active_channels: ['slack'], channel_count: 1,
      slack: { connected: true, enabled: true },
    });
    expect(Object.keys(out)).toEqual(['slack']);
  });

  it('survives a missing or malformed payload', () => {
    expect(channelMap(undefined)).toEqual({});
    expect(channelMap(null)).toEqual({});
    expect(channelMap({})).toEqual({});
  });
});
