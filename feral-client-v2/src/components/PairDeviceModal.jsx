import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Bluetooth, QrCode, KeyRound, Smartphone, Terminal, RefreshCw } from 'lucide-react';
import QRCode from 'qrcode';
import Modal from '../ui/Modal';
import Tabs from '../ui/Tabs';
import DeviceQRCode from '../ui/DeviceQRCode';
import CopyButton from '../ui/CopyButton';
import { apiFetch, apiJson } from '../lib/api';

/**
 * PairDeviceModal: three pairing flows and one radio check.
 *
 *   1) Web phone — generates a /pair?t=<TOKEN> URL + QR. Any phone
 *      camera scans it, lands on Pair.jsx, one tap = live browser_node.
 *      NO app install. This is the default tab.
 *   2) Daemon token — generates a pairing token + shows a copy-paste
 *      one-liner for the Python node SDK and the phone-bridge daemon.
 *      Vendors use this.
 *   3) QR (native app) — legacy host+port+token JSON for the iOS /
 *      Android app.
 *   4) Bluetooth: a Web BLE radio + range check for THIS browser tab.
 *      Explicitly not a pairing flow; see BLETab for why the brain
 *      cannot be handed a browser-held BLE device.
 *
 * All three real flows share one shape, and it is the definition of
 * "paired" everywhere in FERAL: POST /api/devices/pair mints a token
 * row, the device then attaches to /v1/node over HUP and claims that
 * token (POST /api/devices/pair/complete -> mark_claimed). Nothing is
 * paired until a device holds a socket. Consequently this modal never
 * reports a completed pair itself: completion arrives over the
 * WebSocket, and the parent refreshes its list when the modal closes.
 *
 * Token hygiene contract: every token issued by this modal is tracked.
 * On close, any token that was NOT claimed (no `claimed_at` on its
 * paired row) is revoked via DELETE /api/devices/{id}. That kills the
 * "open the modal, never scan, leave a phantom row in Paired" bug.
 */
export default function PairDeviceModal({ open, onClose, onTokenIssued }) {
  const [tab, setTab] = useState('web_phone');
  const [closing, setClosing] = useState(false);
  // Tokens issued during this modal session, plus any in-flight
  // /pair/url requests we must await before pruning.
  const issued = useRef(new Set());
  const inFlight = useRef(new Set());

  const trackIssue = useCallback((promise) => {
    inFlight.current.add(promise);
    promise
      .then((body) => {
        if (body?.device_id) {
          issued.current.add(body.device_id);
          // Notify the parent so it can hide this row from the
          // historical Paired list until pairing actually completes
          // (or the modal closes and the row is revoked). This is
          // what removes the "ghost row appears the moment I click
          // Pair a device" effect users complained about.
          onTokenIssued?.(body.device_id);
        }
      })
      .catch(() => {})
      .finally(() => { inFlight.current.delete(promise); });
  }, [onTokenIssued]);

  // Reset session bookkeeping every time the modal re-opens so a
  // previous session's tracked ids can't leak into a later prune.
  useEffect(() => {
    if (open) {
      issued.current = new Set();
      inFlight.current = new Set();
    }
  }, [open]);

  const handleClose = useCallback(async () => {
    if (closing) return;
    setClosing(true);
    try {
      // Wait for any in-flight token requests to land — we don't want
      // a token created mid-close to escape the prune.
      if (inFlight.current.size > 0) {
        await Promise.allSettled([...inFlight.current]);
      }
      const ids = [...issued.current];
      if (ids.length > 0) {
        let claimedById = new Map();
        try {
          const body = await apiJson('/api/devices/paired');
          const rows = body?.devices || [];
          claimedById = new Map(rows.map((d) => [d.device_id || d.id, !!d.claimed_at]));
        } catch { /* offline — fall through and best-effort revoke */ }
        await Promise.allSettled(ids.map((id) => {
          if (claimedById.get(id) === true) return Promise.resolve();
          return apiFetch(`/api/devices/${encodeURIComponent(id)}`, { method: 'DELETE' });
        }));
        issued.current = new Set();
      }
    } finally {
      setClosing(false);
      onClose?.();
    }
  }, [closing, onClose]);

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title="Pair a device"
      size="md"
      dismissible={!closing}
    >
      <Tabs
        value={tab}
        onChange={setTab}
        items={[
          { id: 'web_phone', label: 'Web phone' },
          { id: 'daemon', label: 'Daemon token' },
          { id: 'app_qr', label: 'Native app QR' },
          { id: 'ble', label: 'Bluetooth check' },
        ]}
      />
      <div className="v2-pair-body">
        <div style={{ display: tab === 'web_phone' ? 'block' : 'none' }}>
          <WebPhoneTab active={tab === 'web_phone'} onIssue={trackIssue} />
        </div>
        <div style={{ display: tab === 'daemon' ? 'block' : 'none' }}>
          <DaemonTokenTab onIssue={trackIssue} />
        </div>
        <div style={{ display: tab === 'app_qr' ? 'block' : 'none' }}>
          <AppQRTab active={tab === 'app_qr'} onIssue={trackIssue} />
        </div>
        <div style={{ display: tab === 'ble' ? 'block' : 'none' }}>
          <BLETab onUseDaemonTab={() => setTab('daemon')} />
        </div>
      </div>
    </Modal>
  );
}

function WebPhoneTab({ active, onIssue }) {
  // Token issuance happens INSIDE the active tab so a user who only
  // wanted to peek at the modal (e.g. opened it from the dock) doesn't
  // leave behind a stray pairing row. The handshake-completion event
  // arrives over the WebSocket, and the parent refreshes its
  // list on modal close, so the pair lands either way.
  //
  // Every issued token is reported to the parent via `onIssue`. The
  // parent prunes any unclaimed tokens on close so a peek-and-leave
  // user never sees ghost rows in the Paired list.
  const [pair, setPair] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [requirePin, setRequirePin] = useState(true);
  const [qrSrc, setQrSrc] = useState(null);

  const generate = useCallback(async () => {
    if (!active) return;
    setBusy(true);
    setError(null);
    const q = new URLSearchParams({ name: 'web-phone' });
    if (requirePin) q.set('pin', 'true');
    const promise = apiJson(`/api/devices/pair/url?${q.toString()}`);
    onIssue?.(promise);
    try {
      const body = await promise;
      setPair(body);
    } catch (err) {
      setError(err?.message || 'failed to generate token');
    } finally {
      setBusy(false);
    }
  }, [active, onIssue, requirePin]);

  useEffect(() => {
    let cancelled = false;
    const url = pair?.url || '';
    if (!url) {
      setQrSrc(null);
      return undefined;
    }
    (async () => {
      try {
        const src = await QRCode.toDataURL(url, {
          errorCorrectionLevel: 'M',
          margin: 2,
          width: 480,
        });
        if (!cancelled) setQrSrc(src);
      } catch {
        if (!cancelled) setQrSrc(null);
      }
    })();
    return () => { cancelled = true; };
  }, [pair?.url]);

  const url = pair?.url || '';

  return (
    <div className="v2-pair-phone-camera" data-testid="pair-web-phone">
      <div className="v2-p v2-p--muted" style={{ marginBottom: 10 }}>
        <Smartphone size={13} style={{ verticalAlign: 'text-bottom', marginRight: 4 }} />
        Open the phone's camera app, point it at this QR. A browser opens,
        they tap "Pair this device" once — phone becomes a real HUP node.
        No app install.
      </div>
      <label
        className="v2-p v2-p--muted"
        style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}
      >
        <input
          type="checkbox"
          checked={requirePin}
          onChange={(e) => setRequirePin(e.target.checked)}
          disabled={busy}
        />
        Require phone PIN confirmation
      </label>
      <button
        type="button"
        className="v2-btn v2-btn--primary"
        onClick={generate}
        disabled={busy}
        data-testid="pair-web-phone-generate"
      >
        {busy ? 'Generating one-time link…' : (url ? 'Regenerate one-time link' : 'Generate one-time link')}
      </button>
      {busy && <div className="v2-chip v2-chip--warn">Generating one-time link…</div>}
      {error && <div className="v2-chip v2-chip--error">{error}</div>}
      {pair?.pin_required && (
        <div className="v2-chip v2-chip--warn" style={{ marginTop: 8 }} data-testid="pair-web-phone-pin">
          PIN: <code style={{ marginLeft: 4 }}>{pair?.pin || 'unavailable'}</code>
        </div>
      )}
      {url && (
        <>
          <div className="v2-pair-qr" style={{ display: 'flex', justifyContent: 'center', marginBottom: 10 }}>
            {qrSrc ? (
              <img
                src={qrSrc}
                alt="Web phone pairing QR code"
                width={240}
                height={240}
                className="v2-qr"
                data-testid="pair-web-phone-qr"
              />
            ) : (
              <div className="v2-qr v2-qr--loading" style={{ width: 240, height: 240 }}>…</div>
            )}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 4 }}>
            <code className="v2-p v2-p--tiny" style={{ flex: 1, wordBreak: 'break-all' }} data-testid="pair-web-phone-url">
              {url}
            </code>
            <CopyButton value={url} label="Copy URL" testId="pair-web-phone-copy" />
            <button type="button" className="v2-btn v2-btn--ghost" onClick={generate} aria-label="New token" disabled={busy}>
              <RefreshCw size={13} />
            </button>
          </div>
        </>
      )}
      <p className="v2-p v2-p--tiny v2-p--muted" style={{ marginTop: 10 }} data-testid="pair-web-phone-hint">
        Scan with your phone camera. Tap Pair when the page opens.
      </p>
      <p className="v2-p v2-p--tiny v2-p--muted" style={{ marginTop: 4 }}>
        Privacy: sensor streams start only after the user taps "Allow".
        Tab-hidden more than 60 s auto-pauses them. Closing the tab tears
        down the WebSocket.
      </p>
    </div>
  );
}

function DaemonTokenTab({ onIssue }) {
  const [pair, setPair] = useState(null);
  const [nodeId, setNodeId] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const generate = useCallback(async () => {
    if (!nodeId.trim()) return;
    setBusy(true);
    setError(null);
    // Same orphan-pruning contract as WebPhoneTab — wrap the request
    // in a promise the parent can track + revoke on close.
    const promise = (async () => {
      const r = await apiFetch('/api/devices/pair', {
        method: 'POST',
        body: JSON.stringify({
          name: nodeId.trim(),
          kind: 'hup',
          node_id: nodeId.trim(),
        }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok || body?.error) {
        const err = new Error(body?.detail || body?.error || `${r.status}`);
        err._body = body;
        err._status = r.status;
        throw err;
      }
      return body;
    })();
    onIssue?.(promise);
    try {
      const body = await promise;
      setPair(body);
    } catch (err) {
      setError(err?.message || 'failed');
    } finally {
      setBusy(false);
    }
  }, [nodeId, onIssue]);

  const brainUrl = typeof window !== 'undefined' ? window.location.origin : '';
  const wsUrl = brainUrl.replace(/^http/, 'ws') + '/v1/node';

  const pythonOneLiner = pair
    ? `pip install feral-node-sdk && python -m feral_node_sdk.cli --node-id "${pair.node_id || nodeId}" --brain-url "${wsUrl}" --token "${pair.token}"`
    : '';
  const bridgeOneLiner = pair
    ? `curl -fsSL ${brainUrl}/install-phone-bridge.sh | bash -s -- --token "${pair.token}" --brain-url "${wsUrl}"`
    : '';

  return (
    <div className="v2-pair-daemon">
      <div className="v2-p v2-p--muted" style={{ marginBottom: 10 }}>
        <KeyRound size={13} style={{ verticalAlign: 'text-bottom', marginRight: 4 }} />
        Issue a pairing token and drop a one-liner on any laptop / server.
        The token authenticates the next WebSocket that attaches with
        matching <code>node_id</code>.
      </div>
      <label className="v2-step-field">
        <span>Node ID (your choice — persists across reboots)</span>
        <input
          className="v2-input"
          value={nodeId}
          onChange={(e) => setNodeId(e.target.value)}
          placeholder="my-laptop-bridge"
        />
      </label>
      <button
        type="button"
        className="v2-btn v2-btn--primary"
        onClick={generate}
        disabled={busy || !nodeId.trim()}
        style={{ marginTop: 8 }}
      >
        {busy ? 'Issuing…' : 'Issue token'}
      </button>

      {error && <div className="v2-chip v2-chip--error" style={{ marginTop: 8 }}>{error}</div>}

      {pair && (
        <div style={{ marginTop: 14, display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div className="v2-chip v2-chip--live">Token issued — copy either one-liner below.</div>

          <OneLiner
            id="py"
            label="Python SDK (any OS)"
            cmd={pythonOneLiner}
          />
          <OneLiner
            id="bridge"
            label="Phone-bridge daemon (Mac / Linux)"
            cmd={bridgeOneLiner}
          />

          <p className="v2-p v2-p--tiny v2-p--muted" style={{ marginTop: 4 }}>
            The token is only shown once. If you lose it, revoke + reissue.
            The copy button confirms only after the clipboard write
            actually succeeded, so a checkmark here means the token is
            really on your clipboard.
          </p>
        </div>
      )}
    </div>
  );
}

function OneLiner({ id, label, cmd }) {
  return (
    <div className="v2-publish-cli" id={`one-${id}`}>
      <div className="v2-publish-cli-label">{label}</div>
      <div className="v2-publish-cli-row">
        <Terminal size={13} aria-hidden="true" />
        <code>{cmd}</code>
        <CopyButton value={cmd} label="Copy command" testId={`one-${id}-copy`} />
      </div>
    </div>
  );
}

function AppQRTab({ active, onIssue }) {
  const [generated, setGenerated] = useState(false);
  const [nonce, setNonce] = useState(0);

  const handleTokenIssued = useCallback((deviceId) => {
    if (!deviceId) return;
    onIssue?.(Promise.resolve({ device_id: deviceId }));
  }, [onIssue]);

  const generate = useCallback(() => {
    if (!active) return;
    setGenerated(true);
    setNonce((n) => n + 1);
  }, [active]);

  return (
    <div className="v2-pair-qr">
      <button
        type="button"
        className="v2-btn v2-btn--primary"
        onClick={generate}
        data-testid="pair-app-qr-generate"
      >
        {generated ? 'Regenerate native app QR' : 'Generate native app QR'}
      </button>
      {generated && (
        <div style={{ marginTop: 10 }}>
          <DeviceQRCode key={nonce} size={240} mode="app" onTokenIssued={handleTokenIssued} />
        </div>
      )}
      <div className="v2-p v2-p--muted">
        <QrCode size={13} style={{ verticalAlign: 'text-bottom', marginRight: 4 }} />
        Scan from the FERAL iOS or Android app. The QR encodes
        <code> &#123;host, port, token&#125;</code> — the app connects to
        the Brain and registers as a real HUP node.
      </div>
    </div>
  );
}

/**
 * BLETab: a radio + range check. NOT a pairing flow, and it no longer
 * says it is.
 *
 * What it used to do: call `navigator.bluetooth.requestDevice()`,
 * which only opens the browser's device chooser, and then immediately
 * fire `onPaired({source:'ble', ...})`. The parent
 * (Devices.jsx:245 handlePaired) closed the modal and refreshed the
 * paired list. So the user saw the "device paired" gesture complete
 * and the list come back without their device in it, because nothing
 * was ever paired: no `gatt.connect()`, no call to the brain, nothing
 * persisted. The caveat line explaining the limitation was unmounted
 * by the very callback that rendered the claim.
 *
 * Why it is not fixed by "just calling the brain" (investigated
 * before relabelling):
 *
 *   - The brain's pairing model is token-then-attach.
 *     `POST /api/devices/pair` (api/routes/devices.py:780) accepts
 *     kind in {name, hup, browser, browser_node_v2, pending} and 400s
 *     on anything else. It mints a row; the row is only paired once
 *     something attaches to /v1/node and claims the token via
 *     `POST /api/devices/pair/complete` -> `mark_claimed`. A
 *     `BluetoothDevice` handle cannot open a WebSocket, so it cannot
 *     claim anything.
 *   - The one path a BLE peripheral does take into the brain is the
 *     `peripheral_bridge_register` HUP frame (api/server.py:3099),
 *     sent over an already-authenticated node socket. See
 *     pages/phone/PeripheralsPanel.jsx:33 for the real call site: the
 *     phone is itself a paired node and bridges its peripherals. The
 *     brain registers a `BridgedPeripheralAdapter(node_id=...)` and
 *     relays every later read/write back through that node's socket.
 *     There is no equivalent for this modal, which runs in the
 *     operator console, not in a node.
 *   - Adding an HTTP endpoint would not help. Whatever the brain
 *     recorded, it could not read a characteristic without this tab
 *     relaying it, and the handle dies on refresh, navigation, or the
 *     modal simply closing. A row that survives its own transport is
 *     the ghost-row bug this file's token hygiene contract exists to
 *     prevent.
 *
 * So the honest outcome is: keep the scan, which genuinely answers
 * "does this machine have a working BLE radio and is the peripheral in
 * range", drop the pairing claim, and point at the two flows that do
 * pair a bridge.
 */
function BLETab({ onUseDaemonTab }) {
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState(null);
  const [device, setDevice] = useState(null);

  const supported = typeof navigator !== 'undefined' && 'bluetooth' in navigator;

  const scan = async () => {
    setError(null);
    setScanning(true);
    try {
      const dev = await navigator.bluetooth.requestDevice({
        acceptAllDevices: true,
        optionalServices: ['battery_service', 'heart_rate', 'device_information'],
      });
      // Deliberately terminal. The result of this scan is a fact about
      // this browser tab, and it is reported here rather than handed
      // to the parent as a pairing event.
      setDevice(dev);
    } catch (err) {
      if (err?.name !== 'NotFoundError') setError(err?.message || 'BLE scan failed');
    } finally {
      setScanning(false);
    }
  };

  if (!supported) {
    return (
      <div className="v2-p v2-p--muted" data-testid="pair-ble-unsupported">
        <Bluetooth size={13} style={{ verticalAlign: 'text-bottom', marginRight: 4 }} />
        Web Bluetooth isn't available in this browser. Use Chrome / Edge
        on a machine with a BLE radio, or use the desktop app for
        production BLE scanning.
      </div>
    );
  }

  return (
    <div className="v2-pair-ble" data-testid="pair-ble">
      <div className="v2-p v2-p--muted" style={{ marginBottom: 10 }}>
        <Bluetooth size={13} style={{ verticalAlign: 'text-bottom', marginRight: 4 }} />
        Checks that this machine has a working BLE radio and that a
        peripheral is in range. It does not add the peripheral to FERAL:
        the handle the browser returns lives in this tab only and is
        gone on refresh, and the brain has no way to talk to it.
      </div>
      <button type="button" className="v2-btn v2-btn--primary" onClick={scan} disabled={scanning}>
        {scanning ? 'Scanning…' : 'Check for BLE devices'}
      </button>
      {device && (
        <div
          className="v2-pair-picked"
          data-testid="pair-ble-result"
          style={{
            marginTop: 10,
            padding: 10,
            display: 'flex',
            flexDirection: 'column',
            gap: 6,
            border: '1px solid var(--v2-hairline)',
            borderRadius: 'var(--v2-radius-sm)',
            background: 'var(--v2-surface-0)',
          }}
        >
          <strong>{device.name || device.id}</strong>
          <span className="v2-p v2-p--muted v2-p--tiny">
            In range and reachable from this browser. Not registered with
            the brain, and not in your device list.
          </span>
          <span className="v2-p v2-p--muted v2-p--tiny">
            To let FERAL use it, run something that bridges it: pair a
            phone from the Web phone tab and add the peripheral there, or
            issue a daemon token and run the phone-bridge daemon on a
            machine near the peripheral.
          </span>
          {onUseDaemonTab && (
            <div>
              <button type="button" className="v2-btn" onClick={onUseDaemonTab}>
                Issue a daemon token
              </button>
            </div>
          )}
        </div>
      )}
      {error && <div className="v2-chip v2-chip--error">{error}</div>}
    </div>
  );
}
