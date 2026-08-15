/**
 * DeviceTopology — demo-pretty visualization of the live FERAL mesh.
 *
 * Renders the brain at the center, with the iPhone node, browser
 * client, and any other top-level connected nodes orbiting it; each
 * node's verified BLE / USB sub-devices (W300 glasses, Veepoo
 * wristband, etc.) hang off their parent so the operator can see at
 * a glance which physical sensor is feeding each surface.
 *
 * Rendered with inline SVG for the connector lines and absolutely-
 * positioned glass cards for the nodes themselves so the cards keep
 * their full Glass / StatusDot / chip styling without a separate
 * `<foreignObject>` dance. Pure presentation — all data comes from
 * `/api/devices/connected` (parent nodes + their sub-devices) and
 * `/api/dashboard` (live HR/SpO2 badges).
 *
 * Demo prep 2026-06-05: this is the surface that goes on camera
 * during the flagship demo, so it MUST stay fully consistent with
 * the v2 design system (Glass, Pane, StatusDot, v2-chip, tokens).
 */

import React, { useMemo } from 'react';
import { Activity, Brain, Eye, Globe, Smartphone, Watch, HardDrive, Heart, Wind } from 'lucide-react';
import Glass from '../ui/Glass';
import StatusDot from '../ui/StatusDot';

/** Pick an icon + human label for a connected node. */
function nodeVisual(node) {
  const type = (node?.type || '').toLowerCase();
  const name = (node?.name || node?.node_id || '').toLowerCase();
  if (type === 'iphone' || name.includes('iphone') || name.includes('ios')) {
    return { Icon: Smartphone, label: 'iPhone', tone: 'phone' };
  }
  if (type === 'browser' || name.includes('browser') || name.includes('chrome') || name.includes('safari')) {
    return { Icon: Globe, label: 'Browser', tone: 'browser' };
  }
  if (type === 'wearable') {
    return { Icon: Watch, label: 'Wearable', tone: 'wearable' };
  }
  if (type === 'glasses' || name.includes('glasses') || name.includes('w300')) {
    return { Icon: Eye, label: 'Glasses', tone: 'glasses' };
  }
  return { Icon: HardDrive, label: node?.type || 'Node', tone: 'generic' };
}

/** Pick an icon for a sub-device row. */
function subVisual(sub) {
  const cap = (sub?.capability || '').toLowerCase();
  // The brain's grouped record carries the peripheral's own reported
  // name ("W300", "VITRO"). Prefer it: "Glasses" is what class of thing
  // it is, "W300" is which one, and the user recognises the latter.
  const name = (sub?.name || '').trim();
  if (cap.includes('glass') || cap.includes('hud') || cap.includes('w300')) {
    return { Icon: Eye, label: name || 'Glasses' };
  }
  if (cap.includes('wrist') || cap.includes('veepoo') || cap.includes('watch')) {
    return { Icon: Watch, label: name || 'Wristband' };
  }
  if (cap.includes('heart') || cap.includes('hr')) {
    return { Icon: Heart, label: name || 'HR sensor' };
  }
  if (cap.includes('spo2') || cap.includes('oxy')) {
    return { Icon: Wind, label: name || 'SpO2 sensor' };
  }
  return { Icon: Activity, label: name || sub?.capability || 'sensor' };
}

/**
 * "5 days ago" from the brain's `last_seen_age_s`. Returns '' when the
 * brain did not supply an age, because inventing one from a missing
 * field is how a surface ends up asserting freshness it cannot back.
 */
export function ageText(ageS) {
  if (typeof ageS !== 'number' || !Number.isFinite(ageS) || ageS <= 0) return '';
  if (ageS < 90) return `${Math.round(ageS)} s ago`;
  if (ageS < 5400) return `${Math.round(ageS / 60)} min ago`;
  if (ageS < 172800) return `${Math.round(ageS / 3600)} h ago`;
  return `${Math.floor(ageS / 86400)} days ago`;
}

/**
 * Hover text for a grouped peripheral chip. `observations` and
 * `also_seen_via` exist because the brain collapses one physical
 * peripheral seen through several install-scoped node ids into a
 * single row; the detail stays reachable so grouping never hides
 * history.
 */
function subTooltip(sub) {
  const parts = [`${sub?.capability || 'peripheral'} · ${sub?.status || 'unknown'}`];
  const age = ageText(sub?.last_seen_age_s);
  if (age) parts.push(`last seen ${age}`);
  if (sub?.via_node_id) parts.push(`via ${sub.via_node_id}`);
  const n = sub?.observations;
  if (typeof n === 'number' && n > 1) {
    parts.push(`${n} observations across ${(sub.also_seen_via || []).length + 1} installs`);
  }
  return parts.join('\n');
}

/**
 * Render whatever the caller handed us as `unreadable` into one line
 * of operator-readable text. Accepts an ApiError (`detail`), a plain
 * Error (`message`), a string, or `true` for "it failed and I have
 * nothing more to tell you".
 */
function unreadableText(err) {
  if (typeof err === 'string' && err.trim()) return err.trim();
  const detail = err?.detail || err?.message;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  return 'The device list could not be fetched from the brain.';
}

function finiteNumber(v) {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/**
 * The single device-count derivation for every surface that reads
 * `/api/dashboard`. Home, GlassBrain and HubLauncher each grew their
 * own fallback chain and disagreed: Home read
 * `online_count`/`paired_count`, GlassBrain read the legacy
 * `device_count` under a bare "Devices" label (so it showed `0` while
 * Home showed `0/3`), and HubLauncher had a third chain again.
 *
 * What the brain actually returns (`feral-core/api/routes/dashboard.py`
 * `_get_dashboard_data`, the `return {...}` at the end):
 *
 *   device_count         = len(state.daemons)   (legacy, live-only)
 *   online_count         = len(state.daemons)   (same number, new name)
 *   paired_count         = len(pairing_store.list_devices())
 *   paired_offline_count = paired rows with no open socket
 *   paired_unavailable   = str | null, set when the pairing store threw
 *   devices[]            = the live rows PLUS the paired-but-offline rows
 *
 * So `device_count` and `online_count` are the same value and neither
 * one is a device total: a paired phone that backgrounded itself is in
 * neither. The honest total is the length of `devices[]`, which is
 * `online_count + paired_offline_count`. We take the max of that and
 * `paired_count` because the two are counted from different stores: a
 * daemon can hold a socket without a pairing row (legacy / unclaimed
 * node), in which case `paired_count` alone under-reports.
 *
 * Returns `null` counts when there is no payload at all. That is not
 * the same as zero, and callers must render it as unknown rather than
 * picking a number the brain never gave them.
 */
export function deviceCounts(payload) {
  if (!payload || typeof payload !== 'object') {
    return { online: null, offline: null, total: null, unavailable: null, known: false };
  }
  const online = finiteNumber(payload.online_count)
    ?? finiteNumber(payload.device_count)
    ?? 0;
  const paired = finiteNumber(payload.paired_count);
  const offline = finiteNumber(payload.paired_offline_count)
    ?? Math.max((paired ?? online) - online, 0);
  const total = Math.max(paired ?? 0, online + offline);
  return {
    online,
    offline,
    total,
    unavailable: payload.paired_unavailable ?? null,
    known: true,
  };
}

/**
 * Pull the live HR/SpO2 reading off `/api/dashboard.latest_health`.
 * The brain only sets `heart_rate` / `spo2` (without `_stale`) when
 * the sample is fresh enough to count as "current" — see the
 * freshness gate in `api/routes/dashboard.py`.
 */
function liveBadges(latestHealth) {
  if (!latestHealth || typeof latestHealth !== 'object') return [];
  const out = [];
  if (latestHealth.heart_rate && latestHealth.heart_rate_fresh) {
    out.push({
      key: 'hr',
      Icon: Heart,
      value: `${Math.round(latestHealth.heart_rate)} bpm`,
      source: latestHealth.heart_rate_source || '',
    });
  }
  if (latestHealth.spo2 && latestHealth.spo2_fresh) {
    out.push({
      key: 'spo2',
      Icon: Wind,
      value: `${Math.round(latestHealth.spo2)}%`,
      source: latestHealth.spo2_source || '',
    });
  }
  return out;
}

/**
 * Top-level component. `connected` is the array returned by
 * `/api/devices/connected`; `latestHealth` is `/api/dashboard
 * .latest_health` (HR + SpO2 with fresh/stale flags).
 *
 * `unreadable` is how a caller says "I could not read the device
 * lists". Pass the error (an Error, an ApiError, or a plain string);
 * anything falsy means the empty arrays are a real answer. It exists
 * because an empty `connected` used to render "Awaiting node, pair an
 * iPhone or browser daemon to populate the mesh" for BOTH "the brain
 * says you have no devices" and "the fetch failed", which is an
 * affirmative negative the component had no evidence for. Devices.jsx
 * worked around it by suppressing the whole component on a failed
 * load; that fixed one caller and left the component itself lying to
 * every other mount.
 */
export default function DeviceTopology({
  connected = [], offline = [], latestHealth = null, unreadable = null,
}) {
  // Live nodes first, then the ones that dropped. `offline` comes from
  // the brain's new `/api/devices/connected.offline[]`: before it
  // existed, a node that disconnected was popped from `state.daemons`
  // and simply vanished from this orbit, so the last thing the operator
  // ever saw for it was a green pulsing dot. Absence read as "no such
  // device" instead of "your device dropped".
  const nodes = useMemo(
    () => [
      ...(Array.isArray(connected) ? connected : []),
      ...(Array.isArray(offline) ? offline : []),
    ],
    [connected, offline],
  );
  const badges = useMemo(() => liveBadges(latestHealth), [latestHealth]);

  // Always render: the demo wants the brain card visible even when
  // the orbit is empty, so the operator can see the mesh root before
  // any node has connected. We only hide the orbit container when
  // there are zero nodes so we don't draw a stub connector to nowhere.
  return (
    <Glass level={2} radius="lg" padding="lg" className="v2-topology">
      <header className="v2-pane-header">
        <h2 className="v2-pane-title">Topology</h2>
        <div className="v2-pane-actions">
          {badges.map((b) => (
            <span
              key={b.key}
              className="v2-chip v2-chip--accent"
              title={b.source ? `source: ${b.source}` : undefined}
              data-testid={`v2-topology-badge-${b.key}`}
            >
              <b.Icon size={11} style={{ verticalAlign: 'text-bottom' }} />
              <span style={{ marginLeft: 4 }}>{b.value}</span>
            </span>
          ))}
        </div>
      </header>

      <div className="v2-topology-body" data-testid="v2-topology">
        <div className="v2-topology-center">
          <Glass level={1} radius="md" padding="md" className="v2-topology-brain">
            <Brain size={28} />
            <div className="v2-topology-brain-label">FERAL Brain</div>
            <div className="v2-topology-brain-sub">localhost:9090</div>
          </Glass>
        </div>

        <div className="v2-topology-orbit">
          {/* Unreadable wins over empty. "No devices" is a claim about
              the world; "we could not read the device list" is a claim
              about the fetch, and only the second one is true when the
              request failed. The warning renders even with nodes
              present (a partial read is still a partial read). */}
          {unreadable ? (
            <div
              className="v2-topology-empty"
              data-testid="v2-topology-unreadable"
              title={unreadableText(unreadable)}
            >
              <span className="v2-chip v2-chip--warn">
                <StatusDot tone="warn" label="Device list unreadable" />
                <span style={{ marginLeft: 4 }}>
                  Device list unreadable. The mesh below may be incomplete or out of date.
                </span>
              </span>
              <div
                className="v2-p v2-p--tiny v2-p--muted"
                style={{ marginTop: 6, color: 'var(--v2-text-tertiary)' }}
              >
                {unreadableText(unreadable)}
              </div>
            </div>
          ) : nodes.length === 0 && (
            <div className="v2-topology-empty" data-testid="v2-topology-empty">
              <span className="v2-chip v2-chip--muted">
                Awaiting node — pair an iPhone or browser daemon to populate the mesh.
              </span>
            </div>
          )}
          {nodes.map((node) => {
            const { Icon, label } = nodeVisual(node);
            const subs = Array.isArray(node.subdevices) ? node.subdevices : [];
            // `connected !== false` and not `!!connected`: a brain that
            // predates the flag omits it, and an old brain's live-only
            // payload is genuinely all-live. Only an explicit false
            // means "this node dropped".
            const isLive = node.connected !== false;
            const age = ageText(node.last_seen_age_s);
            const olderInstalls = Array.isArray(node.also_known_as) ? node.also_known_as : [];
            return (
              <div
                key={node.node_id || node.name || label}
                className={`v2-topology-branch${isLive ? '' : ' is-offline'}`}
                data-testid="v2-topology-branch"
              >
                <div className="v2-topology-link" aria-hidden="true" />
                <Glass
                  level={1}
                  radius="md"
                  padding="md"
                  className="v2-topology-node"
                >
                  <header className="v2-device-head">
                    {/* Was a hardcoded `tone="live" pulse`. It was true
                        only because this list held open sockets and
                        nothing else; it is now fed disconnected nodes
                        too, and a green pulsing dot on a phone that
                        dropped five days ago is exactly the complaint. */}
                    <StatusDot
                      tone={isLive ? 'live' : 'off'}
                      pulse={isLive}
                      label={`${node.name || node.node_id || label} ${isLive ? 'connected' : 'disconnected'}`}
                    />
                    <Icon size={14} />
                    <h3 className="v2-device-name">
                      {node.name || node.node_id || label}
                    </h3>
                  </header>
                  <div className="v2-device-meta">{label}</div>
                  <div
                    className={`v2-device-meta${isLive ? '' : ' v2-p--muted'}`}
                    data-testid="v2-topology-node-state"
                  >
                    {isLive
                      ? 'Connected'
                      : `Disconnected${age ? ` · last seen ${age}` : ''}`}
                  </div>
                  {olderInstalls.length > 0 && (
                    <div
                      className="v2-device-meta v2-p--muted"
                      data-testid="v2-topology-node-aka"
                      title={olderInstalls.join('\n')}
                    >
                      {`Same device as ${olderInstalls.length} earlier install${olderInstalls.length === 1 ? '' : 's'}`}
                    </div>
                  )}

                  {subs.length > 0 && (
                    <div className="v2-topology-subs">
                      {subs.map((s, si) => {
                        const sv = subVisual(s);
                        return (
                          <div
                            key={s.capability ? `${s.capability}-${s.name || si}` : si}
                            className="v2-topology-sub"
                            data-testid="v2-topology-sub"
                            title={subTooltip(s)}
                          >
                            <span className="v2-topology-sub-link" aria-hidden="true" />
                            <span className="v2-chip">
                              <StatusDot
                                tone={s.live ? 'live' : 'off'}
                                pulse={!!s.live}
                                label={`${sv.label} ${s.live ? 'live' : 'stale'}`}
                              />
                              <sv.Icon size={11} style={{ marginLeft: 4 }} />
                              <span style={{ marginLeft: 4 }}>{sv.label}</span>
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  )}

                  {/* Deliberately text, not a button. The brain has no
                      outbound channel to a node that is not holding a
                      WebSocket, so a "Reconnect" control would do
                      nothing. `reconnect.steps` is authored by the
                      brain so the UI and the model say the same thing. */}
                  {!isLive && Array.isArray(node.reconnect?.steps) && node.reconnect.steps.length > 0 && (
                    <div
                      className="v2-p v2-p--tiny v2-p--muted"
                      data-testid="v2-topology-reconnect"
                      title={node.reconnect.why || undefined}
                      style={{ marginTop: 6 }}
                    >
                      {node.reconnect.steps[0]}
                    </div>
                  )}
                </Glass>
              </div>
            );
          })}
        </div>
      </div>
    </Glass>
  );
}
