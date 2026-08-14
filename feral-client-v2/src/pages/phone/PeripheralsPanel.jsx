import React, { useMemo, useState } from "react";
import { useOutletContext } from "react-router-dom";

function detectPlatform() {
  const ua = typeof navigator !== "undefined" ? navigator.userAgent || "" : "";
  const isIOS = /iPhone|iPad|iPod/i.test(ua)
    || (/Macintosh/i.test(ua) && typeof navigator !== "undefined" && navigator.maxTouchPoints > 1);
  const hasBluetooth = typeof navigator !== "undefined" && !!navigator.bluetooth;
  return { ua, isIOS, hasBluetooth };
}

export default function PeripheralsPanel({ shell: shellProp }) {
  const outletShell = useOutletContext();
  const shell = shellProp || outletShell || {};
  const [devices, setDevices] = useState([]);
  const [error, setError] = useState("");

  const platform = useMemo(() => detectPlatform(), []);

  const addDevice = async () => {
    if (!navigator.bluetooth?.requestDevice) return;
    setError("");
    try {
      const device = await navigator.bluetooth.requestDevice({
        acceptAllDevices: true,
        optionalServices: ["battery_service", "device_information", "heart_rate"],
      });
      setDevices((prev) => {
        const next = prev.filter((entry) => entry.id !== device.id);
        next.push({ id: device.id, name: device.name || device.id });
        return next;
      });
      shell?.sendFrame?.("peripheral_bridge_register", {
        bridge_id: `phone-bridge-${shell.deviceId || "unknown"}`,
        platform: platform.isIOS ? "ios" : "android",
        devices: [{
          device_id: device.id,
          kind: "unknown",
          protocol: "web_bluetooth",
          capabilities: [],
          status: "connected",
          manifest: {},
        }],
      });
    } catch (err) {
      if (err?.name === "NotFoundError") return;
      setError(err?.message || "Unable to add device.");
    }
  };

  if (platform.isIOS && !platform.hasBluetooth) {
    return (
      <section data-testid="peripherals-panel">
        <p>
          Web Bluetooth not supported on iOS — use the FERAL iOS app.
        </p>
      </section>
    );
  }

  if (!platform.hasBluetooth) {
    return (
      <section data-testid="peripherals-panel">
        <p>
          Web Bluetooth is unavailable in this browser. Use Android Chrome to bridge BLE peripherals.
        </p>
      </section>
    );
  }

  return (
    <section data-testid="peripherals-panel" style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <button
        type="button"
        className="v2-btn v2-btn--primary"
        onClick={addDevice}
      >
        Add device
      </button>
      {error ? <div className="v2-chip v2-chip--error">{error}</div> : null}
      <div
        style={{
          borderRadius: 10,
          border: "1px solid var(--v2-hairline)",
          background: "var(--v2-fill-subtle)",
          padding: 10,
          minHeight: 120,
        }}
      >
        {devices.length === 0 ? (
          <p className="v2-p v2-p--muted" style={{ margin: 0 }}>
            No bridged peripherals yet.
          </p>
        ) : (
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {devices.map((device) => (
              <li key={device.id}>{device.name}</li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
