import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Plus, RefreshCw, Zap, Radio, Wifi, Trash2, Sparkles, Check, Ban } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Modal from '../ui/Modal';
import StatusDot from '../ui/StatusDot';
import EmptyState from '../ui/EmptyState';
import ErrorState from '../ui/ErrorState';
import PairDeviceModal from '../components/PairDeviceModal';
import PerceptionShare from '../components/PerceptionShare';
import DeviceTopology, { ageText } from '../components/DeviceTopology';
import { apiJson, apiFetch } from '../lib/api';
import { useFeralSocket } from '../hooks/useFeralSocket';
import { firstRejection } from '../hooks/useResource';

/**
 * Enter / Space activation for the `role="button" tabIndex={0}` cards
 * below. They were focusable but had no key handler, so a keyboard user
 * could land on a device card and had no way to open its detail modal:
 * the whole modal was mouse-only. A native <button> is not available here
 * because the card is a Glass panel that also needs a hover transform,
 * and nesting interactive controls inside a button is invalid.
 *
 * The `e.target !== e.currentTarget` guard stops a nested control's own
 * Enter/Space from bubbling up and opening the card at the same time.
 * preventDefault on Space stops the page scrolling, which is what a
 * native button does.
 */
function activateOnKey(fn) {
  return (e) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
      e.preventDefault();
      fn();
    }
  };
}

// Labels we refuse to render verbatim — they are placeholders from
// before commit 5 renamed the pair-QR default. Replaced by a
// kind + short-id composite.
const PLACEHOLDER_NAMES = new Set([
  'phone', 'unnamed', 'browser_camera_share', 'device', '',
]);

function labelFor(row) {
  // The brain now resolves this: `label` says what the row IS ("iPhone",
  // "Browser", "Pairing code (unclaimed)") from the claimant's platform
  // rather than from the transport that carried the token. Every one of
  // the 61 rows on the audited install said kind='browser', including
  // the ones an iPhone claimed, which is why a phone presented as a
  // browser connection the owner never made. Client-side reconstruction
  // stays below for older brains.
  if (row?.label) return row.label;
  const raw = (row?.name || '').trim();
  if (raw && !PLACEHOLDER_NAMES.has(raw.toLowerCase())) return raw;
  const kind = row?.kind || row?.type;
  const shortId = (row?.device_id || row?.id || '').slice(0, 8);
  if (kind && shortId) return `${kind} · ${shortId}`;
  if (kind) return kind;
  if (shortId) return shortId;
  return 'unnamed pairing';
}

/**
 * Build the hover tooltip for a sub-device chip. Renders the canonical
 * truth fields the brain's NodeSubdeviceStore exposes — capability,
 * status, provenance, last-seen age, and the heartbeat window that
 * drives the live↔stale derate. Truthful even when the row is stale:
 * the user can read why the dot is grey.
 */
function subdeviceTooltip(s) {
  if (!s) return '';
  const parts = [];
  parts.push(`${s.capability || 'subdevice'} · ${s.status || 'unknown'}`);
  if (s.provenance) parts.push(`provenance: ${s.provenance}`);
  if (typeof s.last_seen === 'number') {
    const ageS = Math.max(0, (Date.now() / 1000) - s.last_seen);
    parts.push(`last seen ${ageS < 60 ? `${Math.round(ageS)} s` : `${Math.round(ageS / 60)} min`} ago`);
  }
  if (typeof s.liveness_window_s === 'number') {
    parts.push(`heartbeat window ${Math.round(s.liveness_window_s)} s`);
  }
  if (s.attrs && typeof s.attrs === 'object') {
    if (s.attrs.device_name) parts.push(`device: ${s.attrs.device_name}`);
    if (s.attrs.battery_level != null) parts.push(`battery: ${s.attrs.battery_level}%`);
  }
  return parts.join('\n');
}

/**
 * Capability names, whatever shape the brain sent.
 *
 * Three shapes are live at once: `/api/devices/connected` sends a list
 * of strings straight off the daemon's node_register payload,
 * `/api/hardware/mesh` sends an object keyed by capability, and
 * `/api/hardware/device/{id}` sends a list of full capability manifests
 * (`{id, name, description, category, ...}`). The last one used to be
 * rendered with `String(c)`, so the detail modal for any node in the
 * device registry printed a row of `[object Object]` where its
 * capabilities should have been.
 */
function capListOf(source) {
  if (Array.isArray(source)) {
    return source.map((c) => {
      if (typeof c === 'string') return c;
      if (c && typeof c === 'object') return String(c.id || c.name || c.capability || '').trim() || 'capability';
      return String(c);
    }).filter(Boolean);
  }
  if (source && typeof source === 'object') return Object.keys(source);
  return [];
}

/**
 * The one card every pane on this page renders.
 *
 * Before this, the four lists each drew their own card with their own
 * content, so a Live card measured 343x149 (type, manufacturer, model,
 * five capability chips, a "Haptic: unwired" chip and one chip per
 * sub-device), a Paired card measured 520x162 (chips plus its own Revoke
 * button) and a HUP mesh card measured 343x87. Three panes, three sizes,
 * and the widest card carried more text than the summary it was
 * supposed to be.
 *
 * Every card is now the same fixed size and carries exactly three
 * things: a status dot, a name, and one line of meta. Everything that
 * used to be printed on the card is in the detail modal the card opens,
 * which is reachable by click and by Enter/Space.
 */
function DeviceCard({
  tone, pulse, statusLabel, name, meta, onOpen, ariaLabel, testid,
}) {
  return (
    <Glass
      level={0}
      radius="md"
      padding="sm"
      className="v2-device-card"
      data-testid={testid}
      onClick={onOpen}
      onKeyDown={activateOnKey(onOpen)}
      role="button"
      tabIndex={0}
      aria-label={ariaLabel}
      title={`${name}\n${meta}`}
    >
      <header className="v2-device-head">
        <StatusDot tone={tone} pulse={pulse} label={statusLabel} />
        <h3 className="v2-device-name">{name}</h3>
      </header>
      <div className="v2-device-meta">{meta}</div>
    </Glass>
  );
}

/**
 * One line of summary, built from counts rather than from a list of
 * chips. `3 capabilities` is the same fact as three chips and costs one
 * card row instead of three.
 */
function summarize(parts) {
  return parts.filter(Boolean).join(' · ');
}

function countLabel(n, singular, plural) {
  if (!n) return '';
  return `${n} ${n === 1 ? singular : (plural || `${singular}s`)}`;
}

/**
 * Devices — live + paired + HUP mesh. Click a device for detail +
 * actuator invoke. Uses Brain's real endpoints:
 *   GET /api/devices/connected   <- live daemon WebSockets, real types
 *   GET /api/devices/paired      <- historical pairing tokens (claimed)
 *   GET /api/devices/paired?include_unclaimed=true <- + unclaimed tokens
 *   GET /api/hardware/mesh       <- HUP mesh snapshot
 *   GET /api/hardware/device/{id}
 *   POST /api/hardware/invoke
 *   DELETE /api/devices/{device_id}
 */
export default function Devices() {
  const [connected, setConnected] = useState([]);
  // Nodes the brain knows about that are NOT holding a socket. Before
  // `/api/devices/connected.offline[]` existed, a phone that dropped was
  // popped from `state.daemons` and vanished from every pane on this
  // page, so the last state the user saw for it was a live green dot and
  // nothing ever contradicted that.
  const [offline, setOffline] = useState([]);
  const [paired, setPaired] = useState([]);
  // Unclaimed pairing tokens. `/api/devices/paired` filters them out by
  // default (deliberately — they are pairing codes, not devices), which
  // meant `unclaimed` in the Paired pane was computed over a list that by
  // construction contained none, so the "Clear unclaimed" button was
  // permanently disabled and could never prune anything. The count comes
  // from the endpoint that actually returns those rows.
  const [unclaimedCount, setUnclaimedCount] = useState(0);
  const [mesh, setMesh] = useState([]);
  // 2026-06-05 demo prep — Topology view also wants the live HR/SpO2
  // numbers off /api/dashboard.latest_health so the brain card can
  // surface a green chip for fresh wearable readings. We poll it on
  // the same cadence as the device lists and hide stale samples.
  const [latestHealth, setLatestHealth] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showPair, setShowPair] = useState(false);
  const [selected, setSelected] = useState(null);
  // `error` = a mutation the user asked for failed (revoke, prune).
  // `loadError` = we could not read the device lists at all, which is
  // the difference between "you have no devices" and "we do not know".
  const [error, setError] = useState(null);
  const [loadError, setLoadError] = useState(null);
  // Device IDs that the currently-open PairDeviceModal session has
  // requested via /api/devices/pair{,/url}. The Brain creates the row
  // immediately so the next /api/devices/paired poll picks it up,
  // which is what made a "phantom row" appear the moment the user
  // clicked "+ Pair new device" — the modal was visually trapped
  // below the dock (z-index regression, see styles/_z.css) so all the
  // user perceived was a row materializing without explanation.
  //
  // We hold these IDs out of the displayed list until either:
  //   - the row's claimed_at flips truthy (= pair flow completed), or
  //   - the modal closes (= the modal revokes the unclaimed token, so
  //     the next refresh poll won't return it anyway).
  const [pendingPairIds, setPendingPairIds] = useState(() => new Set());

  const socket = useFeralSocket();

  const refresh = useCallback(async () => {
    // `Promise.allSettled` never rejects. The old body wrapped it in a
    // try/catch, so the `catch` was dead code and `setError(null)` ran
    // unconditionally on every tick, including the tick where all four
    // calls had just failed. With the brain down the page therefore
    // rendered "No devices paired yet" plus a "Pair your first device"
    // CTA, which is an assertion about hardware the client never got to
    // ask about. Inspect each settled result instead.
    const [c, p, m, d, u] = await Promise.allSettled([
      apiJson('/api/devices/connected'),
      apiJson('/api/devices/paired'),
      apiJson('/api/hardware/mesh'),
      apiJson('/api/dashboard'),
      apiJson('/api/devices/paired?include_unclaimed=true'),
    ]);
    if (c.status === 'fulfilled') {
      setConnected(c.value?.devices || []);
      setOffline(c.value?.offline || []);
    }
    if (p.status === 'fulfilled') setPaired(p.value?.devices || []);
    if (m.status === 'fulfilled') setMesh(m.value?.nodes || []);
    if (d.status === 'fulfilled') {
      setLatestHealth(d.value?.latest_health || null);
    }
    if (u.status === 'fulfilled') {
      const rows = u.value?.devices || [];
      setUnclaimedCount(rows.filter((r) => !r.claimed_at && !r.last_seen).length);
    }
    // The three device-list endpoints are what the "no devices" empty
    // state speaks for. If ANY of them failed we do not know the list
    // is empty, so the page must not say it is. The unclaimed count is
    // not one of them: it arms a button, it does not speak for the list.
    setLoadError(firstRejection([c, p, m]));
    // `error` is now only ever a mutation failure (revoke / prune). It
    // used to be cleared here on every 10s tick, which also silently
    // wiped the message `forget` had just set, because `forget` ends by
    // awaiting this refresh. Each mutation clears it on entry instead.
    setLoading(false);
  }, []);

  // Phase-1 real-time sub-device deltas. The brain emits
  // `subdevice_update` and `subdevice_remove` over /v1/session every
  // time the truth store mutates (ingest, liveness derate, recovery).
  // Without this hook the only way the dashboard would notice was
  // the 15 s `refresh` interval — long enough for a glasses
  // disconnect to look "Active" for up to a quarter-minute.
  useEffect(() => {
    const unsub = socket.subscribe((msg) => {
      if (!msg || msg.type !== 'state_push') return;
      const evt = msg.event;
      const data = msg.data;
      if (!data || typeof data !== 'object') return;
      if (evt === 'subdevice_update') {
        setConnected((prev) => prev.map((d) => {
          if (d.node_id !== data.node_id) return d;
          const others = (d.subdevices || []).filter(
            (s) => s.capability !== data.capability,
          );
          return { ...d, subdevices: [data, ...others] };
        }));
      } else if (evt === 'subdevice_remove') {
        setConnected((prev) => prev.map((d) => {
          if (d.node_id !== data.node_id) return d;
          const remaining = (d.subdevices || []).filter(
            (s) => s.capability !== data.capability,
          );
          return { ...d, subdevices: remaining };
        }));
      }
    });
    return unsub;
  }, [socket]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10000);
    return () => clearInterval(t);
  }, [refresh]);

  // Drop any pending IDs that the Brain now reports as claimed —
  // those rows must show up in the historical list so the user sees
  // the freshly-paired device land. Unclaimed pending IDs stay
  // hidden until handlePairClose triggers a revoke + refresh.
  useEffect(() => {
    setPendingPairIds((prev) => {
      if (prev.size === 0) return prev;
      let changed = false;
      const next = new Set(prev);
      for (const row of paired) {
        const id = row.device_id || row.id;
        if (id && next.has(id) && (row.claimed_at || row.last_seen)) {
          next.delete(id);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [paired]);

  const visiblePaired = useMemo(
    () => paired.filter((row) => {
      const id = row.device_id || row.id;
      if (!id) return true;
      if (!pendingPairIds.has(id)) return true;
      // A pending row only stays in the list once pairing completed.
      return !!(row.claimed_at || row.last_seen);
    }),
    [paired, pendingPairIds],
  );

  const nothingVisible = connected.length === 0
    && offline.length === 0
    && visiblePaired.length === 0
    && mesh.length === 0;

  const handleTokenIssued = useCallback((deviceId) => {
    if (!deviceId) return;
    setPendingPairIds((prev) => {
      if (prev.has(deviceId)) return prev;
      const next = new Set(prev);
      next.add(deviceId);
      return next;
    });
  }, []);

  const handlePairClose = useCallback(() => {
    setShowPair(false);
    // Drop any IDs the modal session created — the modal already
    // revokes unclaimed tokens before invoking onClose, so the next
    // refresh poll will reflect the truth from the Brain.
    setPendingPairIds(new Set());
    refresh();
  }, [refresh]);

  const handlePaired = useCallback((row) => {
    // Successful pair: the row should show up in the historical
    // list. Clear the suppression so the next render picks it up,
    // close the modal, and trigger a refresh.
    if (row?.device_id) {
      setPendingPairIds((prev) => {
        if (!prev.has(row.device_id)) return prev;
        const next = new Set(prev);
        next.delete(row.device_id);
        return next;
      });
    } else {
      setPendingPairIds(new Set());
    }
    setShowPair(false);
    refresh();
  }, [refresh]);

  // AUDIT-r14 finding 06 fix: forget used to dispatch the DELETE
  // and assume success. A failed delete (404 / 403) left the row
  // visible with no error surfaced. Now we wait for the response,
  // surface non-OK as a chip, and only re-fetch on a successful
  // delete (refresh on failure too so the user sees the truth).
  const forget = async (id) => {
    if (!window.confirm(`Forget device ${id}? This removes the pairing.`)) return;
    setError(null);
    try {
      const r = await apiFetch(`/api/devices/${encodeURIComponent(id)}`, { method: 'DELETE' });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        setError(body?.detail || body?.error || `delete returned ${r.status}`);
      }
    } catch (e) {
      setError(e?.message || 'delete failed');
    } finally {
      setSelected(null);
      refresh();
    }
  };

  return (
    <div className="v2-page v2-page--stack" data-testid="v2-marker">
      <Pane
        title="Devices"
        actions={(
          <>
            <button type="button" className="v2-btn v2-btn--ghost" onClick={refresh} aria-label="Refresh"><RefreshCw size={13} /></button>
            <button type="button" className="v2-btn v2-btn--primary" onClick={() => setShowPair(true)}>
              <Plus size={13} /> Pair new device
            </button>
          </>
        )}
      >
        {error && <div className="v2-chip v2-chip--error">{error}</div>}
        {loading && <EmptyState title="Scanning…" />}

        {!loading && nothingVisible && loadError && (
          <ErrorState
            error={loadError}
            what="your devices"
            hint="The device lists did not load, so we cannot tell you what is paired. Nothing has been unpaired. Retry once the brain is reachable."
            onRetry={refresh}
          />
        )}

        {!loading && nothingVisible && !loadError && (
          <EmptyState
            title="No devices paired yet"
            hint="Pair an iPhone, wristband, smart glasses, or any HUP daemon. FERAL sees their sensors + fires their actuators."
            action={<button type="button" className="v2-btn v2-btn--primary" onClick={() => setShowPair(true)}>Pair your first device</button>}
          />
        )}
        {!loading && !nothingVisible && (
          <p className="v2-p v2-p--muted">Click any device for its full detail, sub-devices and actuator console.</p>
        )}
      </Pane>

      <PerceptionShare />

      {/* Topology draws "Awaiting node, pair an iPhone or browser
          daemon to populate the mesh" over an empty orbit. That is the
          same affirmative negative as the empty state, so it is
          suppressed while the device lists are unreadable. */}
      {!loading && !(nothingVisible && loadError) && (
        <DeviceTopology connected={connected} offline={offline} latestHealth={latestHealth} />
      )}

      {connected.length > 0 && (
        <Pane title={`Live (${connected.length})`}>
          <p className="v2-p v2-p--muted">
            Devices currently holding an open HUP WebSocket. Types come from each daemon's
            <code style={{ margin: '0 4px' }}>node_register</code> payload — never fabricated.
          </p>
          <div className="v2-device-grid">
            {connected.map((d, i) => {
              const name = d.name || d.node_id || 'Device';
              const caps = capListOf(d.capabilities);
              const subs = Array.isArray(d.subdevices) ? d.subdevices : [];
              const liveSubs = subs.filter((s) => s.live).length;
              return (
                <DeviceCard
                  key={d.node_id || i}
                  testid="v2-devices-live-card"
                  tone="live"
                  pulse
                  statusLabel={`${name} connected`}
                  name={name}
                  meta={summarize([
                    d.type || 'unknown',
                    countLabel(caps.length, 'capability', 'capabilities'),
                    subs.length ? `${liveSubs}/${subs.length} sub-devices live` : '',
                  ])}
                  ariaLabel={`${name}, connected. Open details`}
                  onOpen={() => setSelected({ ...d, _source: 'connected' })}
                />
              );
            })}
          </div>
        </Pane>
      )}

      {offline.length > 0 && (
        <Pane title={`Disconnected (${offline.length})`}>
          <p className="v2-p v2-p--muted">
            Devices the brain knows about that are not reporting right now. They
            used to disappear from this page entirely when their WebSocket
            dropped, so the last thing you saw was a live dot. The brain cannot
            reconnect them itself; only the device can start that.
          </p>
          <div className="v2-device-grid">
            {offline.map((d, i) => {
              const name = d.name || d.node_id || 'Device';
              const age = ageText(d.last_seen_age_s);
              return (
                <DeviceCard
                  key={d.node_id || i}
                  testid="v2-devices-offline-card"
                  tone="off"
                  pulse={false}
                  statusLabel={`${d.node_id} disconnected`}
                  name={name}
                  meta={summarize([
                    d.type || 'unknown',
                    'disconnected',
                    age ? `last seen ${age}` : '',
                  ])}
                  ariaLabel={`${name}, disconnected. Open details`}
                  onOpen={() => setSelected({ ...d, _source: 'offline' })}
                />
              );
            })}
          </div>
        </Pane>
      )}

      {visiblePaired.length > 0 && (
        <PairedPane
          paired={visiblePaired}
          unclaimedCount={unclaimedCount}
          onSelect={(d) => setSelected({ ...d, _source: 'paired' })}
          onRefresh={refresh}
        />
      )}

      {mesh.length > 0 && (
        <Pane title={`HUP mesh (${mesh.length})`}>
          <div className="v2-device-grid">
            {mesh.map((n, i) => {
              const name = n.name || n.node_id || 'Node';
              // `/api/hardware/mesh` returns only nodes holding an open
              // socket, and now says `online: true` on each row. Older
              // brains omit the key; treat a missing flag as unknown
              // rather than as offline, since this list is by definition
              // the connected set.
              const isOnline = n.online !== false;
              return (
                <DeviceCard
                  key={n.node_id || i}
                  testid="v2-devices-mesh-card"
                  tone={isOnline ? 'live' : 'off'}
                  pulse={isOnline}
                  statusLabel={`${name} ${isOnline ? 'online' : 'offline'}`}
                  name={name}
                  meta={summarize([
                    n.node_type || n.type || 'node',
                    `HUP ${n.hup_version || '1.x'}`,
                    n.signal != null ? `${Math.round(n.signal)}% signal` : '',
                  ])}
                  ariaLabel={`${name}, ${isOnline ? 'online' : 'offline'}. Open details`}
                  onOpen={() => setSelected({ ...n, _source: 'mesh' })}
                />
              );
            })}
          </div>
        </Pane>
      )}

      <PairDeviceModal
        open={showPair}
        onClose={handlePairClose}
        onPaired={handlePaired}
        onTokenIssued={handleTokenIssued}
      />
      {selected && <DeviceDetailModal device={selected} onClose={() => setSelected(null)} onForget={forget} />}
    </div>
  );
}

function PairedPane({ paired, unclaimedCount, onSelect, onRefresh }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const clearUnclaimed = async () => {
    if (unclaimedCount === 0) return;
    const msg = unclaimedCount === 1
      ? 'Clear 1 unclaimed pairing?'
      : `Clear ${unclaimedCount} unclaimed pairings?`;
    if (!window.confirm(msg)) return;
    setBusy(true);
    setErr(null);
    try {
      // apiFetch throws on any non-2xx, so the old `if (!r.ok)` branch
      // here was unreachable and a failed prune rejected out of this
      // function unhandled: the pane said nothing and `onRefresh` never
      // ran, so the rows the user just tried to clear stayed put with
      // no explanation once the 6s toast expired.
      await apiFetch('/api/devices/pair/prune', {
        method: 'POST',
        body: JSON.stringify({ older_than_seconds: 0 }),
      });
    } catch (e) {
      setErr(e?.detail || e?.message || 'prune failed');
    } finally {
      onRefresh();
      setBusy(false);
    }
  };

  return (
    <Pane
      title={`Paired (${paired.length})`}
      actions={(
        <button
          type="button"
          className="v2-btn v2-btn--ghost"
          onClick={clearUnclaimed}
          disabled={busy || unclaimedCount === 0}
          data-testid="v2-devices-clear-unclaimed"
          title="Revoke every pairing token that was never claimed by a live device"
        >
          <Sparkles size={13} /> Clear unclaimed{unclaimedCount ? ` (${unclaimedCount})` : ''}
        </button>
      )}
    >
      <p className="v2-p v2-p--muted">
        Historical pairings — tokens issued via pair flow. A device can be paired but not
        currently connected. Tokens that never completed a
        <code style={{ margin: '0 4px' }}>/pair/complete</code> handshake are not listed
        here; the button above prunes them.
      </p>
      {err && <div className="v2-chip v2-chip--error">{err}</div>}
      <div className="v2-device-grid">
        {paired.map((d, i) => {
          const claimed = !!(d.claimed_at || d.last_seen);
          const caps = capListOf(d.capabilities);
          const name = labelFor(d);
          return (
            <DeviceCard
              key={d.device_id || d.id || i}
              testid="v2-devices-paired-card"
              tone={claimed ? 'neutral' : 'off'}
              pulse={false}
              statusLabel={`${name} ${claimed ? 'claimed' : 'unclaimed'}`}
              name={name}
              meta={summarize([
                (d.type || d.kind) || '—',
                claimed ? 'claimed' : 'unclaimed',
                d.is_device === false ? 'not a device' : '',
                countLabel(caps.length, 'capability', 'capabilities'),
              ])}
              ariaLabel={`${name}, ${claimed ? 'claimed' : 'unclaimed'}. Open details`}
              onOpen={() => onSelect(d)}
            />
          );
        })}
      </div>
    </Pane>
  );
}

function DetailRow({ label, children }) {
  return (
    <div className="v2-setting-row">
      <div className="v2-setting-label"><div>{label}</div></div>
      <div className="v2-setting-control">{children}</div>
    </div>
  );
}

function DeviceDetailModal({ device, onClose, onForget }) {
  const [detail, setDetail] = useState(device);
  // A failed enrichment used to be indistinguishable from a device with
  // no extra detail, because the brain answered 200 {"error": ...} and
  // the client set that object AS the device.
  const [detailError, setDetailError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [invoke, setInvoke] = useState({ method: '', args: '{}' });
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  // HUP_SPEC.md section 6 says per-device capability gating lives at
  // Settings > Devices > <device> > Capabilities. This panel used to
  // render the capability list as read-only chips, so the spec named a
  // screen that had no control on it and the brain echoed the node's own
  // self-declaration back as `granted_capabilities`.
  const [grants, setGrants] = useState(null);
  const [grantsError, setGrantsError] = useState(null);
  const [grantBusy, setGrantBusy] = useState('');

  const id = device.device_id || device.node_id || device.id;
  // Grants are keyed by node_id, the HUP identity. A paired row that
  // never attached a socket has no node_id and therefore nothing to gate.
  const nodeId = device.node_id || detail.node_id || '';

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    setDetailError(null);
    apiJson(`/api/hardware/device/${encodeURIComponent(id)}`)
      .then((body) => {
        if (cancelled) return;
        // Never let an enrichment lookup destroy the row we already had.
        // Older brains answer 200 with an {"error": ...} body for an
        // unknown id; that is not a device, so it does not replace one.
        if (!body || typeof body !== 'object' || body.error) {
          setDetailError(body?.error || 'no mesh record for this id');
          return;
        }
        setDetail({ ...device, ...body });
      })
      .catch((e) => {
        if (cancelled) return;
        // 404 here is normal for a paired row that is not a mesh node.
        setDetailError(e?.status === 404 ? 'not a mesh node' : (e?.detail || e?.message || 'lookup failed'));
      });
    return () => { cancelled = true; };
  }, [id, device]);

  useEffect(() => {
    if (!nodeId) { setGrants(null); return undefined; }
    let cancelled = false;
    setGrantsError(null);
    apiJson(`/api/devices/${encodeURIComponent(nodeId)}/capabilities`)
      .then((body) => {
        if (cancelled) return;
        setGrants(Array.isArray(body?.capabilities) ? body.capabilities : []);
      })
      .catch((e) => {
        if (cancelled) return;
        // Said out loud rather than falling back to read-only chips: a
        // silent fallback is how a security control ends up looking
        // present while doing nothing, which is the defect being fixed.
        setGrantsError(e?.detail || e?.message || 'could not load capability grants');
      });
    return () => { cancelled = true; };
  }, [nodeId]);

  const toggleGrant = async (capability, granted) => {
    setGrantBusy(capability);
    setGrantsError(null);
    try {
      const r = await apiFetch(`/api/devices/${encodeURIComponent(nodeId)}/capabilities`, {
        method: 'POST',
        body: JSON.stringify({ capability, granted }),
      });
      const body = await r.json().catch(() => ({}));
      if (Array.isArray(body?.capabilities)) setGrants(body.capabilities);
      else setGrantsError(body?.detail || body?.error || 'grant change was not applied');
    } catch (e) {
      setGrantsError(e?.detail || e?.message || 'grant change failed');
    } finally {
      setGrantBusy('');
    }
  };

  // AUDIT-r14 finding 06 fix: previously invalid JSON args were
  // silently coerced to `{}` and the actuator fired with no
  // arguments — so the user thought they sent {brightness: 50} and
  // got an undefined result back. Now we validate and surface the
  // parse error before the request leaves the browser.
  const doInvoke = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      let args = {};
      const raw = (invoke.args || '').trim();
      if (raw && raw !== '{}') {
        try {
          args = JSON.parse(raw);
        } catch (parseErr) {
          setError(`Args JSON is malformed: ${parseErr?.message || parseErr}`);
          setBusy(false);
          return;
        }
        if (args === null || typeof args !== 'object' || Array.isArray(args)) {
          setError('Args must be a JSON object.');
          setBusy(false);
          return;
        }
      }
      if (!invoke.method?.trim()) {
        setError('Method name is required.');
        setBusy(false);
        return;
      }
      // The wire contract is {node_id, command, params}; see
      // docs/mintlify/reference/api.mdx and hardware/mesh.py invoke().
      // This used to post {device_id, method, args}: all three keys
      // missed, so the brain invoked node_id="" command="" and answered
      // `Node not connected: ` with nothing after the colon. Every
      // Invoke click for the life of this modal failed that way, and
      // the message blamed the device.
      const r = await apiFetch('/api/hardware/invoke', {
        method: 'POST',
        body: JSON.stringify({ node_id: id, command: invoke.method.trim(), params: args }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok || body?.error || body?.success === false) {
        setError(body?.detail || body?.error || `${r.status}`);
      } else {
        setResult(body);
      }
    } catch (err) {
      setError(err?.detail || err?.message || 'invoke failed');
    } finally {
      setBusy(false);
    }
  };

  const capList = capListOf(detail.capabilities ?? device.capabilities);
  const subdevices = Array.isArray(device.subdevices) ? device.subdevices : [];
  const alsoKnownAs = Array.isArray(device.also_known_as) ? device.also_known_as : [];
  const reconnectSteps = Array.isArray(device.reconnect?.steps) ? device.reconnect.steps : [];
  const claimed = !!(device.claimed_at || device.last_seen);
  const age = ageText(device.last_seen_age_s);
  const title = device._source === 'paired' ? labelFor(device) : (device.name || id || 'Device');

  return (
    <Modal
      open
      onClose={onClose}
      title={title}
      size="lg"
      actions={(
        <>
          <button type="button" className="v2-btn" onClick={onClose}>Close</button>
          <button
            type="button"
            className="v2-btn"
            onClick={() => onForget(id)}
            data-testid="v2-devices-forget"
          >
            <Trash2 size={12} /> {device._source === 'paired' ? 'Revoke pairing' : 'Forget device'}
          </button>
        </>
      )}
    >
      <div className="v2-setting-stack">
        <DetailRow label="ID"><code className="v2-code-inline">{id}</code></DetailRow>
        <DetailRow label="Type">{detail.type || detail.device_type || detail.kind || device.node_type || '—'}</DetailRow>
        <DetailRow label="Source">{device._source || 'unknown'}</DetailRow>
        {device._source === 'connected' && <DetailRow label="Status">Connected{age ? ` · last seen ${age}` : ''}</DetailRow>}
        {device._source === 'offline' && <DetailRow label="Status">Disconnected{age ? ` · last seen ${age}` : ''}</DetailRow>}
        {device._source === 'paired' && (
          <DetailRow label="Status">
            {claimed ? 'Claimed' : 'Unclaimed pairing code'}
            {device.is_device === false && <span className="v2-chip v2-chip--muted" style={{ marginLeft: 6 }}>not a device</span>}
          </DetailRow>
        )}
        {device._source === 'mesh' && (
          <DetailRow label="Mesh">
            <Radio size={10} style={{ verticalAlign: 'text-bottom' }} /> HUP {device.hup_version || '1.x'}
            {device.signal != null && <> · <Wifi size={10} style={{ verticalAlign: 'text-bottom' }} /> {Math.round(device.signal)}%</>}
          </DetailRow>
        )}
        {(device.platform || device.manufacturer || device.model) && (
          <DetailRow label="Hardware">
            {summarize([device.manufacturer, device.model, device.platform])}
          </DetailRow>
        )}
        {device.firmware_version && <DetailRow label="Firmware">{device.firmware_version}</DetailRow>}
        {device.explain && <DetailRow label="Why">{device.explain}</DetailRow>}
        {alsoKnownAs.length > 0 && (
          <DetailRow label="Also known as">
            <div className="v2-device-caps">
              {alsoKnownAs.map((a, i) => <span key={i} className="v2-chip v2-chip--muted">{String(a)}</span>)}
            </div>
          </DetailRow>
        )}
        {grants?.length > 0 && (
          <DetailRow label="Capabilities">
            <div className="v2-device-caps" data-testid="v2-device-capability-grants">
              {grants.map((g) => (
                <button
                  key={g.capability}
                  type="button"
                  className={`v2-chip${g.granted ? '' : ' v2-chip--muted'}`}
                  disabled={grantBusy === g.capability}
                  aria-pressed={!!g.granted}
                  data-testid={`v2-device-capability-${g.capability}`}
                  title={
                    `${g.tier} tier. ${g.granted ? 'Allowed' : 'Denied'} on this device`
                    + `${g.explicit ? '' : ' (default)'}. Click to ${g.granted ? 'deny' : 'allow'}.`
                  }
                  onClick={() => toggleGrant(g.capability, !g.granted)}
                >
                  {g.granted ? <Check size={10} /> : <Ban size={10} />} {g.capability}
                </button>
              ))}
            </div>
            {grantsError && (
              <div className="v2-chip v2-chip--error" data-testid="v2-device-capability-error">
                {String(grantsError)}
              </div>
            )}
          </DetailRow>
        )}
        {!(grants?.length > 0) && capList.length > 0 && (
          // Nothing gateable: no node_id (an unclaimed pairing row, or a
          // mesh entry that never registered), or the grant lookup did
          // not answer. Shown read-only rather than hidden, because
          // losing the capability list is a worse outcome than losing the
          // toggles, and the error below says which case this is.
          <DetailRow label="Capabilities">
            <div className="v2-device-caps">
              {capList.map((c, i) => <span key={i} className="v2-chip">{String(c)}</span>)}
            </div>
            {grantsError && (
              <div className="v2-chip v2-chip--error" data-testid="v2-device-capability-error">
                {String(grantsError)}
              </div>
            )}
          </DetailRow>
        )}
        {device.type === 'wearable' && !capList.includes('haptic') && (
          <DetailRow label="Haptic">
            <span className="v2-chip v2-chip--muted" title="This daemon hasn't declared a haptic capability. For Theora wristbands the production path is the iOS FeralNode bridge which drives Veepoo SDK haptic directly.">
              unwired
            </span>
          </DetailRow>
        )}
        {subdevices.length > 0 && (
          <DetailRow label="Sub-devices">
            <div className="v2-device-caps">
              {subdevices.map((s, si) => (
                <span
                  key={si}
                  className="v2-chip"
                  title={subdeviceTooltip(s)}
                  data-testid="v2-device-subdevice-chip"
                >
                  <StatusDot
                    tone={s.live ? 'live' : 'off'}
                    pulse={s.live}
                    label={`${s.capability} ${s.live ? 'live' : 'stale'}`}
                  />
                  {s.name || s.capability}
                  {s.status && s.status !== 'ready' && (
                    <span className="v2-chip-suffix"> · {s.status}</span>
                  )}
                </span>
              ))}
            </div>
          </DetailRow>
        )}
        {reconnectSteps.length > 0 && (
          <DetailRow label="Reconnect">
            <ol className="v2-p v2-p--tiny v2-p--muted" style={{ margin: 0, paddingLeft: 16 }}>
              {reconnectSteps.map((step, si) => <li key={si}>{step}</li>)}
            </ol>
          </DetailRow>
        )}
        {detailError && (
          <DetailRow label="Mesh record">
            <span className="v2-chip v2-chip--muted" data-testid="v2-devices-detail-note">{detailError}</span>
          </DetailRow>
        )}
      </div>

      <div className="v2-p" style={{ marginTop: 16, fontWeight: 600 }}>
        <Zap size={14} style={{ verticalAlign: 'text-bottom', marginRight: 6 }} />
        Invoke actuator
      </div>
      <form onSubmit={doInvoke} className="v2-setting-stack">
        <label className="v2-setting-row">
          <div className="v2-setting-label"><div>Method</div></div>
          <div className="v2-setting-control">
            <input
              className="v2-input"
              value={invoke.method}
              onChange={(e) => setInvoke((s) => ({ ...s, method: e.target.value }))}
              placeholder="set_brightness, buzz, stream_start, …"
              required
            />
          </div>
        </label>
        <label className="v2-setting-row">
          <div className="v2-setting-label"><div>Args (JSON)</div></div>
          <div className="v2-setting-control" style={{ minWidth: 240, flex: 1 }}>
            <textarea className="v2-code-editor" rows={3} value={invoke.args} onChange={(e) => setInvoke((s) => ({ ...s, args: e.target.value }))} />
          </div>
        </label>
        <div className="v2-forge-actions">
          <button type="submit" className="v2-btn v2-btn--primary" disabled={busy || !invoke.method}>
            {busy ? 'Invoking…' : 'Invoke'}
          </button>
        </div>
      </form>
      {result && <pre className="v2-code">{JSON.stringify(result, null, 2)}</pre>}
      {error && <div className="v2-chip v2-chip--error">{error}</div>}
    </Modal>
  );
}
