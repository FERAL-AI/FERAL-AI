/**
 * Reading GET /api/channels without rendering its envelope as data.
 *
 * `ChannelManager.stats()` answers an envelope that ALSO spreads the
 * per-channel rows to the top level:
 *
 *     {active_channels: [...], channel_count: N, details: {...}, ...rows}
 *
 * Two pages read it and both got it wrong in different ways, which is
 * why this lives here rather than in either of them.
 *
 * Home ended `|| c.value || {}`, so with nothing configured it walked
 * the envelope and rendered `Active_channels off`, `Channel_count off`
 * and `Details off` as if they were channels.
 *
 * Settings read `stats.status_by_channel || stats.channels`, and
 * NEITHER key exists on this payload, so its channel panel listed
 * nothing at all. It also read `stats.active`, which does not exist
 * either: the brain reports `active_channels` as a LIST and
 * `channel_count` as the number.
 *
 * `details` is the canonical map. The last branch keeps the flattened
 * shape working for a brain that predates it, and filters the envelope
 * out by shape rather than by name: a channel row is an object, and the
 * envelope's own fields are an array and a number.
 */
export function channelMap(payload) {
  const p = payload || {};
  const named = p.status_by_channel || p.details || p.channels;
  if (named && typeof named === 'object' && !Array.isArray(named)) return named;
  return Object.fromEntries(
    Object.entries(p).filter(([, v]) => (
      v && typeof v === 'object' && !Array.isArray(v)
    )),
  );
}

/**
 * How many channels are actually connected.
 *
 * The brain reports this as `active_channels`, a list of channel types.
 * `channel_count` is a different number: how many are configured at
 * all, connected or not.
 */
export function activeChannelCount(payload) {
  const list = payload?.active_channels;
  return Array.isArray(list) ? list.length : 0;
}
