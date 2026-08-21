import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Navigate, Outlet, useNavigate, useParams } from "react-router-dom";
import BrowserNode from "../node/BrowserNode";
import Glass from "../ui/Glass";
import Pane from "../ui/Pane";
// Nothing on this page may call useFeralSocket(). It opens a singleton
// WebSocket to /v1/session, the dashboard's chat WS that authenticates
// with the dashboard API key from localStorage. The phone doesn't have
// that key (it has a phone_bearer in IndexedDB), so /v1/session
// auth-fails and the singleton retries forever, surfaced in the live
// phone test as a connect/disconnect storm in the brain log.
//
// The phone already has its own connection (BrowserNode -> /v1/node)
// for live events. A future "phone-side ops stream" component must
// subscribe to BrowserNode's frame stream directly without touching
// the dashboard socket. (This note used to name shell/LiveOpsStream.jsx
// as the component kept out; that component was deleted in 2026.8.12,
// but the constraint applies to any successor.)
import PairTopBar from "./PairTopBar";
import CapabilityTabs from "./CapabilityTabs";
import {
  clearPhoneBearer,
  getLatestPhoneBearer,
  getPhoneBearer,
} from "../lib/phoneBearerStore";

const MAX_LOG_LINES = 120;
const DEFAULT_PERMISSIONS = {
  location: true,
  camera: false,
  mic: false,
};

function readStoredPermissions() {
  try {
    const raw = localStorage.getItem("feral.permissions_selected");
    if (!raw) return DEFAULT_PERMISSIONS;
    const parsed = JSON.parse(raw);
    return {
      location: parsed?.location !== false,
      camera: !!parsed?.camera,
      mic: !!parsed?.mic,
    };
  } catch {
    return DEFAULT_PERMISSIONS;
  }
}

function writeStoredPermissions(next) {
  try {
    localStorage.setItem("feral.permissions_selected", JSON.stringify(next));
  } catch {
    // Ignore private mode and storage failures.
  }
}

function readClaimMarker() {
  try {
    return localStorage.getItem("feral.pair_claim_marker") || "";
  } catch {
    return "";
  }
}

function isConnectedPhase(phase) {
  return [
    "connected",
    "registered",
    "acknowledged",
    "voice_config",
    "mic_streaming",
    "camera_streaming",
  ].includes(phase);
}

function modeLabelFromPermissions(permissions) {
  if (permissions.camera || permissions.mic) return "Interactive mode";
  if (permissions.location) return "Sensor mode";
  return "Manual mode";
}

export default function PairShell() {
  const { device_id: deviceId = "" } = useParams();
  const navigate = useNavigate();

  const [authState, setAuthState] = useState("loading");
  const [bearerRecord, setBearerRecord] = useState(null);
  const [node, setNode] = useState(null);
  const [status, setStatus] = useState("restoring");
  const [logs, setLogs] = useState([]);
  const [permissions, setPermissions] = useState(() => readStoredPermissions());

  const permissionsRef = useRef(permissions);
  const frameListenersRef = useRef(new Set());
  const phaseListenersRef = useRef(new Map());

  const appendLog = useCallback((event, detail = null) => {
    const line = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      at: new Date().toISOString(),
      event,
      detail,
    };
    setLogs((prev) => [line, ...prev].slice(0, MAX_LOG_LINES));
  }, []);

  const emitPhase = useCallback((phase, detail) => {
    const listeners = phaseListenersRef.current.get(phase);
    if (!listeners) return;
    listeners.forEach((listener) => {
      try {
        listener(detail);
      } catch {
        // Listener errors should not break the shell.
      }
    });
  }, []);

  useEffect(() => {
    permissionsRef.current = permissions;
    writeStoredPermissions(permissions);
  }, [permissions]);

  useEffect(() => {
    let cancelled = false;
    if (!deviceId) {
      setAuthState("missing");
      return () => {
        cancelled = true;
      };
    }

    setAuthState("loading");
    (async () => {
      const direct = await getPhoneBearer(deviceId).catch(() => null);
      const latest = direct ? null : await getLatestPhoneBearer().catch(() => null);
      const candidate = direct || (latest?.paired_device_id === deviceId ? latest : null);
      const marker = candidate?.pair_claim_marker || readClaimMarker();

      if (cancelled) return;
      if (!candidate?.phone_bearer || !marker) {
        setBearerRecord(null);
        setAuthState("missing");
        return;
      }

      const normalized = {
        ...candidate,
        paired_device_id: candidate.paired_device_id || deviceId,
        pair_claim_marker: marker,
      };
      setBearerRecord(normalized);
      setAuthState("ready");
      setStatus("restoring");

      try {
        localStorage.setItem("feral.paired_device_id", normalized.paired_device_id);
        localStorage.setItem("feral.pair_claim_marker", marker);
      } catch {
        // Ignore storage failures.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [deviceId]);

  useEffect(() => {
    if (authState !== "ready" || !bearerRecord?.phone_bearer) return undefined;

    let cancelled = false;
    const nodeInstance = new BrowserNode({
      token: bearerRecord.phone_bearer,
      onPhase: (phase, detail) => {
        if (cancelled) return;
        setStatus(phase);

        if (phase === "frame") {
          const frameType = detail?.type || "frame";
          appendLog(`frame:${frameType}`, detail?.payload || null);
          emitPhase(frameType, detail);
          frameListenersRef.current.forEach((listener) => {
            try {
              listener(detail);
            } catch {
              // Ignore listener errors.
            }
          });
          return;
        }

        appendLog(`phase:${phase}`, detail || null);
        emitPhase(phase, detail);
      },
      onError: (err) => {
        setStatus("failed");
        appendLog("error", err?.message || String(err));
      },
    });

    setNode(nodeInstance);
    setStatus("connecting");
    appendLog("connect:start", bearerRecord.paired_device_id);

    (async () => {
      try {
        await nodeInstance.connect();
        if (permissionsRef.current.location) {
          await nodeInstance.startSensors({
            location: true,
            camera: false,
            mic: false,
          });
        }
      } catch (err) {
        if (!cancelled) {
          setStatus("failed");
          appendLog("connect:failed", err?.message || String(err));
        }
      }
    })();

    return () => {
      cancelled = true;
      nodeInstance.stop().catch(() => {});
      setNode(null);
    };
  }, [appendLog, authState, bearerRecord?.paired_device_id, bearerRecord?.phone_bearer, emitPhase]);

  useEffect(() => {
    if (!node) return;
    if (!permissions.camera) {
      node.stopCamera().catch(() => {});
      return;
    }
    node.startCamera().catch((err) => {
      appendLog("camera:error", err?.message || String(err));
    });
  }, [appendLog, node, permissions.camera]);

  useEffect(() => {
    if (!node) return;
    if (!permissions.mic) {
      node.stopMic().catch(() => {});
      return;
    }
    node.startMic().catch((err) => {
      appendLog("mic:error", err?.message || String(err));
    });
  }, [appendLog, node, permissions.mic]);

  useEffect(() => {
    if (!node || !permissions.location) return;
    node.startSensors({ location: true, camera: false, mic: false }).catch((err) => {
      appendLog("location:error", err?.message || String(err));
    });
  }, [appendLog, node, permissions.location]);

  const subscribeFrame = useCallback((listener) => {
    if (typeof listener !== "function") return () => {};
    frameListenersRef.current.add(listener);
    return () => {
      frameListenersRef.current.delete(listener);
    };
  }, []);

  const subscribePhase = useCallback((phase, listener) => {
    if (!phase || typeof listener !== "function") return () => {};
    const bucket = phaseListenersRef.current.get(phase) || new Set();
    bucket.add(listener);
    phaseListenersRef.current.set(phase, bucket);
    return () => {
      const current = phaseListenersRef.current.get(phase);
      if (!current) return;
      current.delete(listener);
      if (current.size === 0) {
        phaseListenersRef.current.delete(phase);
      }
    };
  }, []);

  const sendFrame = useCallback((type, payload = {}) => {
    if (!node || typeof node._send !== "function" || !type) return false;
    void node._send(type, payload);
    appendLog(`send:${type}`, payload);
    return true;
  }, [appendLog, node]);

  const setPermission = useCallback((key, enabled) => {
    setPermissions((prev) => {
      const next = { ...prev, [key]: !!enabled };
      writeStoredPermissions(next);
      return next;
    });
    if (key === "location" && !enabled) {
      appendLog("location:disabled", "Location stream stops after reconnect.");
    }
  }, [appendLog]);

  const disconnect = useCallback(async () => {
    try {
      await node?.stop();
    } catch {
      // Ignore disconnect errors.
    }
    try {
      await clearPhoneBearer(deviceId);
    } catch {
      // Ignore store cleanup failures.
    }
    try {
      localStorage.removeItem("feral.paired_device_id");
      localStorage.removeItem("feral.pair_claim_marker");
    } catch {
      // Ignore storage failures.
    }
    navigate("/pair", { replace: true });
  }, [deviceId, navigate, node]);

  const shellContext = useMemo(() => ({
    deviceId,
    bearerRecord,
    node,
    voice_config: {
      mode: node?.voiceProvider === "gemini" ? "gemini_live" : "openai_realtime",
    },
    status,
    isConnected: isConnectedPhase(status),
    permissions,
    setPermission,
    sendFrame,
    subscribeFrame,
    subscribePhase,
    logs,
    disconnect,
  }), [
    bearerRecord,
    deviceId,
    disconnect,
    logs,
    node,
    permissions,
    sendFrame,
    setPermission,
    status,
    subscribeFrame,
    subscribePhase,
  ]);

  if (authState === "loading") {
    return (
      <div className="v2-page">
        <Pane title="Restoring paired session">
          <p className="v2-p v2-p--muted">
            Checking saved phone bearer credentials for this device...
          </p>
        </Pane>
      </div>
    );
  }

  if (authState === "missing") {
    return <Navigate to="/pair" replace />;
  }

  const modeLabel = modeLabelFromPermissions(permissions);

  return (
    <div className="v2-page" data-testid="pair-shell">
      <Glass level={2} radius="lg" padding="md">
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <PairTopBar
            deviceId={deviceId}
            status={status}
            modeLabel={modeLabel}
            onDisconnect={disconnect}
          />
          <CapabilityTabs deviceId={deviceId} />
          <Pane padding="md">
            <Outlet context={shellContext} />
          </Pane>
        </div>
      </Glass>
    </div>
  );
}
