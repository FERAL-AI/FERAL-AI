import React, { useCallback, useEffect, useState } from 'react';
import { MapPin, Plus, Trash2, Crosshair, RefreshCw } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Modal from '../ui/Modal';
import EmptyState from '../ui/EmptyState';
import { apiJson, apiFetch } from '../lib/api';

export default function Geofences() {
  const [fences, setFences] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showNew, setShowNew] = useState(false);
  const [currentLoc, setCurrentLoc] = useState(null);
  const [locBusy, setLocBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const d = await apiJson('/api/geofences');
      // `d.geofences || d || []` looked like a defensive fallback and was
      // the opposite. When the key is absent or null it hands the whole
      // response OBJECT to setFences, and the render calls fences.map,
      // which throws TypeError into the error boundary and takes the
      // shell down with it. The live brain answers
      // {"geofences": [], "fences": []} so it never fired in practice,
      // but any error envelope or a renamed key is a white screen.
      // Anything that is not an array is not a list of fences.
      setFences(Array.isArray(d?.geofences) ? d.geofences
        : Array.isArray(d?.fences) ? d.fences
        : []);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const pushLocation = async () => {
    if (!navigator.geolocation) return;
    setLocBusy(true);
    navigator.geolocation.getCurrentPosition(
      async (pos) => {
        const payload = {
          lat: pos.coords.latitude,
          lng: pos.coords.longitude,
          accuracy_m: pos.coords.accuracy,
          timestamp: pos.timestamp,
        };
        setCurrentLoc(payload);
        await apiFetch('/api/location/update', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        setLocBusy(false);
      },
      () => setLocBusy(false),
      { enableHighAccuracy: true, timeout: 10000 },
    );
  };

  return (
    <div className="v2-page v2-page--stack" data-testid="v2-marker">
      <Pane
        title="Geofences + Location"
        actions={(
          <>
            <button type="button" className="v2-btn" onClick={pushLocation} disabled={locBusy}>
              <Crosshair size={12} /> {locBusy ? 'Locating…' : 'Push my location'}
            </button>
            <button type="button" className="v2-btn v2-btn--primary" onClick={() => setShowNew(true)}>
              <Plus size={13} /> New geofence
            </button>
          </>
        )}
      >
        <p className="v2-p v2-p--muted">
          Geofences fire automations when you enter / leave an area. Brain stores your current location only when you push it.
        </p>
        {currentLoc && (
          <Glass level={0} radius="md" padding="md">
            <div className="v2-stat-label">Current location</div>
            <div className="v2-mem-content">{currentLoc.lat.toFixed(5)}, {currentLoc.lng.toFixed(5)} · ±{Math.round(currentLoc.accuracy_m)}m</div>
          </Glass>
        )}
        {loading && <EmptyState title="Loading…" />}
        {!loading && fences.length === 0 && <EmptyState title="No geofences" />}
        <ul className="v2-mem-list">
          {fences.map((f) => (
            <li key={f.id}>
              <Glass level={0} radius="md" padding="md">
                <div className="v2-flow-card-head">
                  <MapPin size={13} aria-hidden="true" />
                  <div className="v2-flow-card-title">{f.name || f.id}</div>
                  <button type="button" className="v2-btn v2-btn--ghost" onClick={async () => { await apiFetch(`/api/geofences/${f.id}`, { method: 'DELETE' }); refresh(); }}>
                    <Trash2 size={12} />
                  </button>
                </div>
                <div className="v2-mem-meta">{f.lat?.toFixed?.(5)}, {f.lng?.toFixed?.(5)} · r={f.radius}m</div>
              </Glass>
            </li>
          ))}
        </ul>
      </Pane>

      {showNew && <NewFenceModal currentLoc={currentLoc} onClose={() => setShowNew(false)} onCreated={() => { setShowNew(false); refresh(); }} />}
    </div>
  );
}

function NewFenceModal({ currentLoc, onClose, onCreated }) {
  const [name, setName] = useState('');
  const [lat, setLat] = useState(currentLoc?.lat || '');
  const [lng, setLng] = useState(currentLoc?.lng || '');
  const [radius, setRadius] = useState(100);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async () => {
    setBusy(true);
    try {
      const r = await apiFetch('/api/geofences', {
        method: 'POST',
        body: JSON.stringify({ name, lat: Number(lat), lng: Number(lng), radius: Number(radius) }),
      });
      if (!r.ok) setError(`${r.status}`);
      else onCreated();
    } finally { setBusy(false); }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title="New geofence"
      actions={(
        <>
          <button type="button" className="v2-btn" onClick={onClose}>Cancel</button>
          <button type="button" className="v2-btn v2-btn--primary" onClick={submit} disabled={busy || !name || !lat || !lng}>
            {busy ? 'Saving…' : 'Create'}
          </button>
        </>
      )}
    >
      <div className="v2-setting-stack">
        <label className="v2-setting-row"><div className="v2-setting-label"><div>Name</div></div>
          <div className="v2-setting-control"><input className="v2-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Home, Gym, Office…" /></div>
        </label>
        <label className="v2-setting-row"><div className="v2-setting-label"><div>Latitude</div></div>
          <div className="v2-setting-control"><input type="number" step="0.00001" className="v2-input" value={lat} onChange={(e) => setLat(e.target.value)} /></div>
        </label>
        <label className="v2-setting-row"><div className="v2-setting-label"><div>Longitude</div></div>
          <div className="v2-setting-control"><input type="number" step="0.00001" className="v2-input" value={lng} onChange={(e) => setLng(e.target.value)} /></div>
        </label>
        <label className="v2-setting-row"><div className="v2-setting-label"><div>Radius (m)</div></div>
          <div className="v2-setting-control"><input type="number" className="v2-input" value={radius} onChange={(e) => setRadius(e.target.value)} /></div>
        </label>
      </div>
      {error && <div className="v2-chip v2-chip--error">{error}</div>}
    </Modal>
  );
}
