import React, { useEffect, useMemo, useState } from "react";
import { Link, useOutletContext, useParams } from "react-router-dom";
import SduiRenderer, { applySduiPatches } from "../../ui/SduiRenderer";

function makeId(prefix = "push") {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`;
}

function buildScreenId(appId, surfaceId, scope = "phone") {
  return [
    encodeURIComponent(String(appId || "")),
    encodeURIComponent(String(surfaceId || appId || "surface")),
    encodeURIComponent(String(scope || "phone")),
  ].join(":");
}

function normalizePush(frameOrPayload) {
  if (!frameOrPayload) return null;
  const payload = frameOrPayload.payload || frameOrPayload;
  if (!payload.kind) return null;
  const appId = payload.app_id || "unknown";
  const surfaceId = payload.surface_id || payload.app_id || "surface";
  return {
    id: payload.push_id || makeId(payload.kind),
    kind: payload.kind,
    app_id: appId,
    surface_id: surfaceId,
    screen_id: payload.screen_id || buildScreenId(appId, surfaceId),
    title: payload.title || payload.app_id || "App notification",
    body: payload.body || "",
    actions: Array.isArray(payload.actions) ? payload.actions : [],
    sdui: payload.sdui || payload.root || null,
  };
}

function normalizePatch(frameOrPayload) {
  if (!frameOrPayload) return null;
  const payload = frameOrPayload.payload || frameOrPayload;
  const screenId = payload.screen_id || "";
  const patches = Array.isArray(payload.patches) ? payload.patches : [];
  if (!screenId || patches.length === 0) return null;
  return { screen_id: screenId, patches };
}

export default function AppsPanel({ shell: shellProp }) {
  const outletShell = useOutletContext();
  const shell = shellProp || outletShell || {};
  const { device_id: deviceId = "", app_id: routeAppId = "" } = useParams();

  const [notifications, setNotifications] = useState([]);
  const [surfaces, setSurfaces] = useState({});

  useEffect(() => {
    const onPush = (frame) => {
      const push = normalizePush(frame);
      if (!push) return;
      if (push.kind === "notification") {
        setNotifications((prev) => [push, ...prev].slice(0, 24));
      }
      if (push.kind === "interactive") {
        setSurfaces((prev) => ({
          ...prev,
          [push.app_id]: push,
        }));
      }
    };

    const onPatch = (frame) => {
      const patch = normalizePatch(frame);
      if (!patch) return;
      setSurfaces((prev) => {
        const next = { ...prev };
        for (const [appId, surface] of Object.entries(prev)) {
          if (!surface || surface.screen_id !== patch.screen_id) continue;
          next[appId] = {
            ...surface,
            sdui: applySduiPatches(surface.sdui, patch.patches),
          };
        }
        return next;
      });
    };

    if (shell?.subscribePhase) {
      const unsubPush = shell.subscribePhase("genui_push", onPush);
      const unsubPatch = shell.subscribePhase("sdui_patch", onPatch);
      return () => {
        if (typeof unsubPush === "function") unsubPush();
        if (typeof unsubPatch === "function") unsubPatch();
      };
    }
    if (shell?.subscribeFrame) {
      return shell.subscribeFrame((frame) => {
        if (frame?.type === "genui_push") onPush(frame);
        if (frame?.type === "sdui_patch") onPatch(frame);
      });
    }
    return () => {};
  }, [shell]);

  const appIds = useMemo(() => {
    const seen = new Set();
    Object.keys(surfaces).forEach((id) => seen.add(id));
    notifications.forEach((entry) => seen.add(entry.app_id));
    return Array.from(seen);
  }, [notifications, surfaces]);

  const activeAppId = routeAppId || appIds[0] || "";
  const activeSurface = activeAppId ? surfaces[activeAppId] : null;

  const dismissNotification = (id) => {
    setNotifications((prev) => prev.filter((entry) => entry.id !== id));
  };

  const emitAction = (actionId, value) => {
    if (!activeAppId || !shell?.sendFrame) return;
    const activeScreenId = activeSurface?.screen_id
      || buildScreenId(activeAppId, activeSurface?.surface_id || activeAppId, deviceId || "phone");
    shell.sendFrame("genui_event", {
      app_id: activeAppId,
      surface_id: activeSurface?.surface_id || activeAppId,
      screen_id: activeScreenId,
      event_type: "tap",
      action_id: actionId,
      value,
    });
  };

  return (
    <section data-testid="apps-panel" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {notifications.length === 0 ? (
          <p className="v2-p v2-p--muted" style={{ margin: 0 }}>
            No app pushes yet.
          </p>
        ) : (
          notifications.map((notification) => (
            <article
              key={notification.id}
              style={{
                borderRadius: 10,
                border: "1px solid var(--v2-hairline)",
                background: "var(--v2-fill-subtle)",
                padding: 10,
                display: "flex",
                alignItems: "flex-start",
                justifyContent: "space-between",
                gap: 8,
              }}
            >
              <div>
                <strong>{notification.title}</strong>
                {notification.body ? (
                  <p className="v2-p v2-p--muted" style={{ margin: "4px 0 0" }}>
                    {notification.body}
                  </p>
                ) : null}
              </div>
              <button
                type="button"
                className="v2-btn v2-btn--ghost"
                onClick={() => dismissNotification(notification.id)}
              >
                Dismiss
              </button>
            </article>
          ))
        )}
      </div>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        {appIds.map((appId) => (
          <Link
            key={appId}
            to={`/pair/${encodeURIComponent(deviceId)}/apps/${encodeURIComponent(appId)}`}
            className={`v2-btn ${activeAppId === appId ? "v2-btn--primary" : ""}`.trim()}
          >
            {appId}
          </Link>
        ))}
      </div>

      <div
        style={{
          borderRadius: 10,
          border: "1px solid var(--v2-hairline)",
          background: "var(--v2-fill-subtle)",
          minHeight: 200,
          padding: 10,
        }}
      >
        {!activeAppId ? (
          <p className="v2-p v2-p--muted" style={{ margin: 0 }}>
            Interactive app surfaces appear here when a
            <code style={{ margin: "0 4px" }}>genui_push</code>
            frame arrives.
          </p>
        ) : !activeSurface?.sdui ? (
          <p className="v2-p v2-p--muted" style={{ margin: 0 }}>
            App <strong>{activeAppId}</strong> has no interactive surface yet.
          </p>
        ) : (
          <SduiRenderer tree={activeSurface.sdui} onAction={emitAction} />
        )}
      </div>
    </section>
  );
}
