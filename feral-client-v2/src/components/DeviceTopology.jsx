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
  if (cap.includes('glass') || cap.includes('hud') || cap.includes('w300')) {
    return { Icon: Eye, label: 'Glasses' };
  }
  if (cap.includes('wrist') || cap.includes('veepoo') || cap.includes('watch')) {
    return { Icon: Watch, label: 'Wristband' };
  }
  if (cap.includes('heart') || cap.includes('hr')) {
    return { Icon: Heart, label: 'HR sensor' };
  }
  if (cap.includes('spo2') || cap.includes('oxy')) {
    return { Icon: Wind, label: 'SpO2 sensor' };
  }
  return { Icon: Activity, label: sub?.capability || 'sensor' };
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
 */
export default function DeviceTopology({ connected = [], latestHealth = null }) {
  const nodes = useMemo(
    () => (Array.isArray(connected) ? connected : []),
    [connected],
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
          {nodes.length === 0 && (
            <div className="v2-topology-empty" data-testid="v2-topology-empty">
              <span className="v2-chip v2-chip--muted">
                Awaiting node — pair an iPhone or browser daemon to populate the mesh.
              </span>
            </div>
          )}
          {nodes.map((node) => {
            const { Icon, label } = nodeVisual(node);
            const subs = Array.isArray(node.subdevices) ? node.subdevices : [];
            return (
              <div
                key={node.node_id || node.name || label}
                className="v2-topology-branch"
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
                    <StatusDot tone="live" pulse />
                    <Icon size={14} />
                    <h3 className="v2-device-name">
                      {node.name || node.node_id || label}
                    </h3>
                  </header>
                  <div className="v2-device-meta">{label}</div>

                  {subs.length > 0 && (
                    <div className="v2-topology-subs">
                      {subs.map((s, si) => {
                        const sv = subVisual(s);
                        return (
                          <div
                            key={si}
                            className="v2-topology-sub"
                            data-testid="v2-topology-sub"
                          >
                            <span className="v2-topology-sub-link" aria-hidden="true" />
                            <span className="v2-chip">
                              <StatusDot
                                tone={s.live ? 'live' : 'off'}
                                pulse={!!s.live}
                              />
                              <sv.Icon size={11} style={{ marginLeft: 4 }} />
                              <span style={{ marginLeft: 4 }}>{sv.label}</span>
                            </span>
                          </div>
                        );
                      })}
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
