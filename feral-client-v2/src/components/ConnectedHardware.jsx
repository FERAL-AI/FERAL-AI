/**
 * ConnectedHardware — compact "what physical hardware is plugged into
 * the brain right now" pane for the Home page. Public demo surface.
 *
 * Wire:
 *   - GET /api/hardware/fleet (poll on the same 15s cadence Home uses)
 *   - Renders one card per `devices[]` row from the fleet payload.
 *   - Each card surfaces ONLY fields the endpoint actually ships:
 *       name, device_type, connection_type, capability count, and
 *       `last_verified.verified` — the truthful "honesty loop" state.
 *
 * Truthfulness contract: this surface never invents a status string.
 *   * `verified === true`  → green dot + "verified ✓"
 *   * `verified === false` → red dot + "verified ✗"
 *   * `verified === null`  → neutral dot + "unverified"
 *   * `last_verified` null → no chip at all (the honesty loop has not
 *                             reported on this device yet — saying
 *                             anything else would be a lie).
 *
 * Failure mode: any fetch error degrades to a small inline chip; the
 * Home page never crashes because /api/hardware/fleet is down or
 * absent on an older brain build.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Cpu, RefreshCw, ChevronRight, Plug } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import StatusDot from '../ui/StatusDot';
import EmptyState from '../ui/EmptyState';
import { apiJson } from '../lib/api';

const POLL_MS = 15000;

function capabilityCount(device) {
  const caps = Array.isArray(device?.capabilities) ? device.capabilities : [];
  return caps.length;
}

/**
 * Map `last_verified` to a tone/label/pulse triple. Returns null when
 * the field is missing or malformed so the caller can omit the chip
 * entirely — never paint a green dot we can't back with evidence.
 */
function verifyChip(lastVerified) {
  if (!lastVerified || typeof lastVerified !== 'object') return null;
  const v = lastVerified.verified;
  if (v === true) {
    return { tone: 'live', pulse: true, label: 'verified \u2713', testid: 'verified-ok' };
  }
  if (v === false) {
    return { tone: 'error', pulse: false, label: 'verified \u2717', testid: 'verified-fail' };
  }
  if (v === null || v === undefined) {
    // Brain ran the action but did not verify (no verify contract,
    // or verify pending). Show neutral so the operator can tell it
    // apart from a verified-good row at a glance.
    return { tone: 'neutral', pulse: false, label: 'unverified', testid: 'verified-unknown' };
  }
  return null;
}

export default function ConnectedHardware() {
  const [fleet, setFleet] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [lastFetched, setLastFetched] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const data = await apiJson('/api/hardware/fleet');
      setFleet(data || { devices: [] });
      setError(null);
      setLastFetched(Date.now());
    } catch (e) {
      // Graceful degrade: keep the previous payload (if any) so a
      // transient network blip doesn't blank the card mid-demo. Surface
      // the failure as an inline chip instead of crashing Home.
      setError(e?.detail || e?.message || 'hardware fleet fetch failed');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  const devices = Array.isArray(fleet?.devices) ? fleet.devices : [];
  const count = devices.length;

  return (
    <Pane
      title={`Connected Hardware${count ? ` · ${count}` : ''}`}
      actions={(
        <>
          <Link to="/devices" className="v2-btn v2-btn--ghost">
            Manage <ChevronRight size={12} />
          </Link>
          <button
            type="button"
            className="v2-btn v2-btn--ghost"
            onClick={refresh}
            aria-label="Refresh connected hardware"
            title="Re-poll /api/hardware/fleet"
          >
            <RefreshCw size={13} />
          </button>
        </>
      )}
    >
      <p className="v2-p v2-p--muted">
        Physical devices the brain has registered through the hardware
        registry — sensors, actuators, robots. The verify chip is the
        live "honesty loop" result for each device's last action.
      </p>

      {error && (
        <div className="v2-chip v2-chip--error" data-testid="v2-home-hardware-error">
          {error}
        </div>
      )}

      {!loading && count === 0 && !error && (
        <EmptyState
          icon={<Plug size={18} aria-hidden="true" />}
          title="No hardware connected"
          hint="Plug in a CuteBot, BLE peripheral, or any HUP hardware adapter. It will register here automatically."
        />
      )}

      {count > 0 && (
        <div
          className="v2-device-grid"
          data-testid="v2-home-connected-hardware"
        >
          {devices.map((dev, i) => {
            const id = dev.device_id || dev.name || `dev-${i}`;
            const caps = capabilityCount(dev);
            const chip = verifyChip(dev.last_verified);
            return (
              <Glass
                key={id}
                level={0}
                radius="md"
                padding="md"
                className="v2-device-card"
                data-testid="v2-home-hardware-card"
              >
                <header className="v2-device-head">
                  <Cpu size={14} aria-hidden="true" />
                  <h3 className="v2-device-name">{dev.name || dev.device_id || 'Device'}</h3>
                </header>
                <div className="v2-device-meta">
                  {dev.device_type || 'hardware'}
                  {dev.connection_type ? <> · {dev.connection_type}</> : null}
                </div>
                <div className="v2-device-caps" style={{ marginTop: 6 }}>
                  <span className="v2-chip v2-chip--muted">
                    {caps} capabilit{caps === 1 ? 'y' : 'ies'}
                  </span>
                  {chip && (
                    <span
                      className="v2-chip"
                      data-testid={`v2-home-hardware-${chip.testid}`}
                      title={
                        dev.last_verified?.capability
                          ? `last verified: ${dev.last_verified.capability}`
                          : undefined
                      }
                    >
                      <StatusDot
                        tone={chip.tone}
                        pulse={chip.pulse}
                        label={chip.label}
                      />
                      <span style={{ marginLeft: 4 }}>{chip.label}</span>
                    </span>
                  )}
                </div>
              </Glass>
            );
          })}
        </div>
      )}

      {lastFetched && (
        <div
          className="v2-p v2-p--tiny v2-p--muted"
          style={{ marginTop: 8 }}
          data-testid="v2-home-hardware-stamp"
        >
          As of {new Date(lastFetched).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', second: '2-digit' })}
        </div>
      )}
    </Pane>
  );
}
