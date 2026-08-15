import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Plus, RefreshCw, Zap, Radio, Wifi, Trash2, Sparkles } from 'lucide-react';
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
 * because these cards contain their own buttons (Forget, Wake) and
 * nesting interactive controls inside a button is invalid.
 *
 * The `e.target !== e.currentTarget` guard stops a nested button's own
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
 * Build the hover tooltip for a sub-device chip on the Live pane.
 * Renders the canonical truth fields the brain's NodeSubdeviceStore
 * exposes — capability, status, provenance, last-seen age, and the
 * heartbeat window that drives the live↔stale derate. Truthful even
 * when the row is stale: the user can read why the dot is grey.
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
 * Devices — live + paired + HUP mesh. Click a device for detail +
 * actuator invoke. Uses Brain's real endpoints:
 *   GET /api/devices/connected   <- live daemon WebSockets, real types
 *   GET /api/devices/paired      <- historical pairing tokens
 *   GET /api/hardware/mesh       <- HUP mesh snapshot
 *   GET /api/hardware/device/{id}, /api/hardware/context
 *   POST /api/hardware/invoke (or /api/hardware/execute)
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
    const [c, p, m, d] = await Promise.allSettled([
      apiJson('/api/devices/connected'),
      apiJson('/api/devices/paired'),
      apiJson('/api/hardware/mesh'),
      apiJson('/api/dashboard'),
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
    // The three device-list endpoints are what the "no devices" empty
    // state speaks for. If ANY of them failed we do not know the list
    // is empty, so the page must not say it is.
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
            {connected.map((d, i) => (
              <Glass
                key={d.node_id || i}
                level={0}
                radius="md"
                padding="md"
                className="v2-device-card"
                onClick={() => setSelected({ ...d, _source: 'connected' })}
                onKeyDown={activateOnKey(() => setSelected({ ...d, _source: 'connected' }))}
                role="button"
                tabIndex={0}
                aria-label={`${d.name || d.node_id || 'Device'}, connected. Open details`}
              >
                <header className="v2-device-head">
                  <StatusDot tone="live" pulse label={`${d.name || d.node_id || 'Device'} connected`} />
                  <h3 className="v2-device-name">{d.name || d.node_id || 'Device'}</h3>
                </header>
                <div className="v2-device-meta">
                  {d.type || 'unknown'}
                  {d.manufacturer && <> · {d.manufacturer}{d.model ? ` ${d.model}` : ''}</>}
                </div>
                {Array.isArray(d.capabilities) && d.capabilities.length > 0 && (
                  <div className="v2-device-caps">
                    {d.capabilities.slice(0, 5).map((c, ci) => (
                      <span key={ci} className="v2-chip">{String(c)}</span>
                    ))}
                  </div>
                )}
                {d.type === 'wearable' && !(d.capabilities || []).includes('haptic') && (
                  <div className="v2-device-caps" style={{ marginTop: 6 }}>
                    <span className="v2-chip v2-chip--muted" title="This daemon hasn't declared a haptic capability. For Theora wristbands the production path is the iOS FeralNode bridge which drives Veepoo SDK haptic directly.">
                      Haptic: unwired
                    </span>
                  </div>
                )}
                {/* Phase-1: render the brain's truth-store sub-device
                    tree directly under each connected node. Each chip
                    binds to the per-row `live` flag so a row whose
                    last heartbeat is past its provenance window
                    auto-derates to `off`. */}
                {Array.isArray(d.subdevices) && d.subdevices.length > 0 && (
                  <div className="v2-device-caps" style={{ marginTop: 6 }}>
                    {d.subdevices.map((s, si) => (
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
                        {s.capability}
                        {s.status && s.status !== 'ready' && (
                          <span className="v2-chip-suffix"> · {s.status}</span>
                        )}
                      </span>
                    ))}
                  </div>
                )}
              </Glass>
            ))}
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
            {offline.map((d, i) => (
              <Glass
                key={d.node_id || i}
                level={0}
                radius="md"
                padding="md"
                className="v2-device-card"
                data-testid="v2-devices-offline-card"
                onClick={() => setSelected({ ...d, _source: 'offline' })}
                onKeyDown={activateOnKey(() => setSelected({ ...d, _source: 'offline' }))}
                role="button"
                tabIndex={0}
                aria-label={`${d.name || d.node_id || 'Device'}, disconnected. Open details`}
              >
                <header className="v2-device-head">
                  <StatusDot tone="off" pulse={false} label={`${d.node_id} disconnected`} />
                  <h3 className="v2-device-name">{d.name || d.node_id || 'Device'}</h3>
                </header>
                <div className="v2-device-meta">
                  {d.type || 'unknown'}
                  {' · disconnected'}
                  {ageText(d.last_seen_age_s) ? ` · last seen ${ageText(d.last_seen_age_s)}` : ''}
                </div>
                {Array.isArray(d.also_known_as) && d.also_known_as.length > 0 && (
                  <div className="v2-device-meta v2-p--muted" title={d.also_known_as.join('\n')}>
                    {`Same device as ${d.also_known_as.length} earlier install${d.also_known_as.length === 1 ? '' : 's'}`}
                  </div>
                )}
                {Array.isArray(d.subdevices) && d.subdevices.length > 0 && (
                  <div className="v2-device-caps" style={{ marginTop: 6 }}>
                    {d.subdevices.map((s, si) => (
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
                      </span>
                    ))}
                  </div>
                )}
                {Array.isArray(d.reconnect?.steps) && d.reconnect.steps.length > 0 && (
                  <ol className="v2-p v2-p--tiny v2-p--muted" style={{ marginTop: 8, paddingLeft: 16 }}>
                    {d.reconnect.steps.map((step, si) => <li key={si}>{step}</li>)}
                  </ol>
                )}
              </Glass>
            ))}
          </div>
        </Pane>
      )}

      {visiblePaired.length > 0 && (
        <PairedPane
          paired={visiblePaired}
          onSelect={(d) => setSelected({ ...d, _source: 'paired' })}
          onForget={forget}
          onRefresh={refresh}
        />
      )}

      {mesh.length > 0 && (
        <Pane title={`HUP mesh (${mesh.length})`}>
          <div className="v2-device-grid">
            {mesh.map((n, i) => (
              <Glass
                key={n.node_id || i}
                level={0}
                radius="md"
                padding="md"
                className="v2-device-card"
                onClick={() => setSelected({ ...n, _source: 'mesh' })}
                onKeyDown={activateOnKey(() => setSelected({ ...n, _source: 'mesh' }))}
                role="button"
                tabIndex={0}
                aria-label={`${n.name || n.node_id || 'Node'}, ${n.online ? 'online' : 'offline'}. Open details`}
              >
                <header className="v2-device-head">
                  <StatusDot
                    tone={n.online ? 'live' : 'off'}
                    pulse={n.online}
                    label={`${n.name || n.node_id || 'Node'} ${n.online ? 'online' : 'offline'}`}
                  />
                  <h3 className="v2-device-name">{n.name || n.node_id}</h3>
                </header>
                <div className="v2-device-meta">
                  <Radio size={10} style={{ verticalAlign: 'text-bottom' }} /> HUP {n.hup_version || '1.x'}
                  {n.signal != null && <> · <Wifi size={10} style={{ verticalAlign: 'text-bottom' }} /> {Math.round(n.signal)}%</>}
                </div>
                {n.capabilities && (
                  <div className="v2-device-caps">
                    {(Array.isArray(n.capabilities) ? n.capabilities : Object.keys(n.capabilities || {})).slice(0, 5).map((c, ci) => (
                      <span key={ci} className="v2-chip">{String(c)}</span>
                    ))}
                  </div>
                )}
              </Glass>
            ))}
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

function PairedPane({ paired, onSelect, onForget, onRefresh }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const unclaimed = useMemo(
    () => paired.filter((d) => !d.claimed_at && !d.last_seen),
    [paired],
  );

  const clearUnclaimed = async () => {
    if (unclaimed.length === 0) return;
    const msg = unclaimed.length === 1
      ? 'Clear 1 unclaimed pairing?'
      : `Clear ${unclaimed.length} unclaimed pairings?`;
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
          disabled={busy || unclaimed.length === 0}
          title="Revoke every pairing token that was never claimed by a live device"
        >
          <Sparkles size={13} /> Clear unclaimed{unclaimed.length ? ` (${unclaimed.length})` : ''}
        </button>
      )}
    >
      <p className="v2-p v2-p--muted">
        Historical pairings — tokens issued via pair flow. A device can be paired but not
        currently connected. Rows marked "unclaimed" never completed a
        <code style={{ margin: '0 4px' }}>/pair/complete</code> handshake and are safe
        to prune.
      </p>
      {err && <div className="v2-chip v2-chip--error">{err}</div>}
      <div className="v2-device-grid">
        {paired.map((d, i) => {
          const claimed = !!(d.claimed_at || d.last_seen);
          return (
            <Glass
              key={d.device_id || d.id || i}
              level={0}
              radius="md"
              padding="md"
              className="v2-device-card"
            >
              <header
                className="v2-device-head"
                onClick={() => onSelect(d)}
                onKeyDown={activateOnKey(() => onSelect(d))}
                role="button"
                tabIndex={0}
                aria-label={`${labelFor(d)}, ${claimed ? 'claimed' : 'unclaimed'}. Open details`}
                style={{ cursor: 'pointer' }}
              >
                <StatusDot
                  tone={claimed ? 'neutral' : 'off'}
                  pulse={false}
                  label={`${labelFor(d)} ${claimed ? 'claimed' : 'unclaimed'}`}
                />
                <h3 className="v2-device-name">{labelFor(d)}</h3>
              </header>
              <div className="v2-device-meta" title={d.explain || undefined}>
                {(d.type || d.kind) || '—'}
                {!claimed && <> · <span className="v2-chip v2-chip--warn">unclaimed</span></>}
                {/* `is_device === false` is the brain saying "this row is
                    a pairing code nobody ever redeemed", which is what 43
                    of the owner's 61 rows actually are. Counting them as
                    devices is what filled the list with browsers he never
                    paired. */}
                {d.is_device === false && <> · <span className="v2-chip v2-chip--muted">not a device</span></>}
              </div>
              {d.capabilities && (
                <div className="v2-device-caps">
                  {(Array.isArray(d.capabilities) ? d.capabilities : Object.keys(d.capabilities || {})).slice(0, 5).map((c, ci) => (
                    <span key={ci} className="v2-chip">{String(c)}</span>
                  ))}
                </div>
              )}
              <div className="v2-forge-actions" style={{ marginTop: 8 }}>
                <button
                  type="button"
                  className="v2-btn v2-btn--ghost"
                  onClick={(e) => { e.stopPropagation(); onForget(d.device_id || d.id); }}
                  aria-label="Revoke this pairing"
                  title="Delete this pairing token"
                >
                  <Trash2 size={12} /> Revoke
                </button>
              </div>
            </Glass>
          );
        })}
      </div>
    </Pane>
  );
}

function DeviceDetailModal({ device, onClose, onForget }) {
  const [detail, setDetail] = useState(device);
  const [busy, setBusy] = useState(false);
  const [invoke, setInvoke] = useState({ method: '', args: '{}' });
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const id = device.device_id || device.node_id || device.id;

  useEffect(() => {
    if (!id) return;
    apiJson(`/api/hardware/device/${encodeURIComponent(id)}`).then(setDetail).catch(() => {});
  }, [id]);

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
      const r = await apiFetch('/api/hardware/invoke', {
        method: 'POST',
        body: JSON.stringify({ device_id: id, method: invoke.method.trim(), args }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok || body?.error) {
        setError(body?.detail || body?.error || `${r.status}`);
      } else {
        setResult(body);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const caps = detail.capabilities || device.capabilities;
  const capList = Array.isArray(caps) ? caps : Object.keys(caps || {});

  return (
    <Modal
      open
      onClose={onClose}
      title={device.name || id || 'Device'}
      size="lg"
      actions={(
        <>
          <button type="button" className="v2-btn" onClick={onClose}>Close</button>
          <button type="button" className="v2-btn" onClick={() => onForget(id)}>Forget device</button>
        </>
      )}
    >
      <div className="v2-setting-stack">
        <div className="v2-setting-row">
          <div className="v2-setting-label"><div>ID</div></div>
          <div className="v2-setting-control"><code className="v2-code-inline">{id}</code></div>
        </div>
        <div className="v2-setting-row">
          <div className="v2-setting-label"><div>Type</div></div>
          <div className="v2-setting-control">{detail.type || detail.kind || '—'}</div>
        </div>
        <div className="v2-setting-row">
          <div className="v2-setting-label"><div>Source</div></div>
          <div className="v2-setting-control">{device._source || 'unknown'}</div>
        </div>
        {capList.length > 0 && (
          <div className="v2-setting-row">
            <div className="v2-setting-label"><div>Capabilities</div></div>
            <div className="v2-setting-control v2-device-caps">
              {capList.map((c, i) => <span key={i} className="v2-chip">{String(c)}</span>)}
            </div>
          </div>
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
