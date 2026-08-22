import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Sun, Moon, Briefcase, Cloud, CloudRain, Snowflake, Zap, RefreshCw, Plug,
  Sparkles, ChevronRight, Plus,
} from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Orb from '../ui/Orb';
import StatusDot from '../ui/StatusDot';
import EmptyState from '../ui/EmptyState';
import SkillsLauncher, { readPinned, MAX_PINNED } from '../components/SkillsLauncher';
import ResumeCockpit from '../components/ResumeCockpit';
import ForYouToday from '../components/ForYouToday';
import ConnectedHardware from '../components/ConnectedHardware';
import { deviceCounts } from '../components/DeviceTopology';
import { apiJson, apiFetch } from '../lib/api';
import { channelMap } from '../lib/channels';

// Re-exported: it moved to lib/channels.js when Settings turned out to
// have the same bug, and several tests import it from here.
export { channelMap };
import { useSomatic } from '../hooks/useSomatic';
import { useSystemHealth, refreshSystemHealth } from '../hooks/useSystemHealth';
import { useBrainEvents, EVENT_TYPES } from '../hooks/useBrainEvents';
import { useConnectionStatus } from '../hooks/useConnectionStatus';
import { useFeralSocket } from '../hooks/useFeralSocket';

/**
 * Home — unified Ambient + Dashboard surface. Replaces the separate
 * Dashboard and Ambient pages.
 */

const MODES = [
  { id: 'briefing', label: 'Briefing', Icon: Sun },
  { id: 'desk', label: 'Desk', Icon: Briefcase },
  { id: 'wind_down', label: 'Wind-Down', Icon: Moon },
];

const WEATHER_ICON = {
  Clear: Sun, Clouds: Cloud, Rain: CloudRain, Drizzle: CloudRain,
  Snow: Snowflake, Thunderstorm: Zap,
};

function autoModeFromHour(h) {
  if (h >= 5 && h < 9) return 'briefing';
  if (h >= 19 || h < 5) return 'wind_down';
  return 'desk';
}

const SKILL_GLYPH = {
  calendar_google: 'Cal',
  github_api: 'GH',
  spotify_music: '♪',
  coding_tools: '</>',
  code_interpreter: '>_',
  web_search: '?',
  web_actions: '@',
  pdf_reader: 'pdf',
  smart_home_hue: '•',
  messaging_sms: '✉',
  messaging_channels: '⌘',
  notes_memory: 'N',
  weather_current: '☀',
  desktop_automation: '🖱',
  computer_use: '▢',
  gui_computer_use: '▦',
  agentic_computer_use: '∴',
  screen_capture: '◫',
  robot_ext: '▲',
  digital_twin: '⁂',
  system_settings: '⚙',
  subagent: '↻',
  self_introspection: '?',
  workspace_scripts: 'sh',
};

/**
 * Human names for the job kinds /api/jobs returns.
 *
 * The pane rendered `j.kind` straight through, so the chip read
 * "tool_genesis" and, once backgrounded shell commands were added to the
 * aggregator, "background_bash". Those are the brain's internal source
 * names, not words anyone reading a dashboard is looking for.
 *
 * An unknown kind falls back to the raw value rather than being hidden,
 * because a new source appearing unlabelled is a much smaller problem
 * than a new source silently not rendering.
 */
export const JOB_KIND_LABELS = {
  taskflow: 'TaskFlow',
  routine: 'Routine',
  specialist: 'Specialist',
  tool_genesis: 'New tool',
  daemon: 'Device',
  background_bash: 'Shell job',
};

export function jobKindLabel(kind) {
  if (!kind) return 'Job';
  return JOB_KIND_LABELS[kind] || kind;
}


/**
 * Tone + label for one channel row out of GET /api/channels.
 *
 * The row `ChannelManager.stats` builds (channels/base.py:1849-1877) is
 * exactly:
 *
 *     {running, connected, known_chats, degraded, failure_count,
 *      degraded_reason?, access_configured, allowed_sender_count,
 *      allowed_chat_count, pairing_window_open, pending_senders,
 *      bot_username?}
 *
 * There is no `enabled` key, and there never has been. The old
 * expression was `connected ? 'connected' : enabled ? 'starting' :
 * 'off'`, so the middle branch was unreachable and every channel that
 * was not fully connected collapsed onto the single word "off" with a
 * grey dot.
 *
 * That is a status lie in two directions. A channel mid-start
 * (`running: true, connected: false`) reads as off. A channel that
 * crashed into the degraded state, carrying a `degraded_reason` the
 * brain went to the trouble of recording, ALSO reads as off, the
 * identical rendering to a channel the operator never configured. The
 * one row on this page whose job is to say "your Telegram bridge is
 * broken" said "you don't have one".
 *
 * Verified empirically by driving the real `ChannelManager.stats`
 * property with a channel in each state rather than reading the source
 * and hoping.
 */
export function channelState(info) {
  const row = info && typeof info === 'object' ? info : {};
  if (row.degraded === true) {
    return {
      tone: 'error',
      label: 'degraded',
      reason: String(row.degraded_reason || '') || 'The brain reported this channel as degraded.',
    };
  }
  if (row.connected === true) return { tone: 'live', label: 'connected', reason: '' };
  if (row.running === true) {
    return {
      tone: 'warn',
      label: 'starting',
      reason: 'Started, but the channel has not reported a connection yet.',
    };
  }
  return { tone: 'off', label: 'off', reason: '' };
}

export default function Home() {
  const somatic = useSomatic();
  const [time, setTime] = useState(new Date());
  const [mode, setMode] = useState(autoModeFromHour(new Date().getHours()));
  // True once the operator has picked a tab by hand.
  //
  // `refresh` runs every 15s and used to apply `snapshot.suggested_mode`
  // unconditionally, so a tab the user clicked was silently swapped back
  // to whatever the clock suggested within one tick. Measured against a
  // live brain at 23:27 local: clicking "Briefing" made it active, and
  // 18 seconds later the active tab read "Wind-Down" again with no
  // further input. The tabs were effectively unusable outside the
  // window where the clock already agreed with the user.
  //
  // A ref rather than state because `refresh` is a `useCallback([])` and
  // must keep a stable identity across ticks (a state dep here would
  // loop with the useEffect that schedules it).
  const modePinnedRef = useRef(false);
  const pickMode = useCallback((id) => {
    modePinnedRef.current = true;
    setMode(id);
  }, []);
  // AUDIT-r14 finding 03 dedup: Home no longer owns a `/api/dashboard`
  // poll. It subscribes to the shared useSystemHealth store (mirrored
  // into local state for the legacy render path) so that the Shell
  // somatic strip + GlassBrain + Hub launcher all share the same
  // 15s tick. The brain's 5s server cache on /api/dashboard (Lane 06)
  // makes any racey concurrent fetches collapse to one upstream call.
  const sysHealth = useSystemHealth();
  const [dashboard, setDashboard] = useState(null);
  const [skills, setSkills] = useState([]);
  const [channels, setChannels] = useState({});
  const [llm, setLlm] = useState(null);
  const [flows, setFlows] = useState([]);
  const [briefing, setBriefing] = useState(null);
  const [nextEvent, setNextEvent] = useState(null);
  const [windDown, setWindDown] = useState(null);
  const [snapshot, setSnapshot] = useState(null);
  const [pinned, setPinned] = useState(readPinned());
  const [launcherOpen, setLauncherOpen] = useState(false);
  // v2026.5.29 — persist twin Q/A across route navigation. The tile
  // previously stored everything in component-local useState, so
  // tapping any nav link wiped both the question the operator was
  // composing and the last answer. sessionStorage survives unmount
  // (and tab-switches) while clearing on browser-quit.
  const [twinQ, setTwinQ] = useState(() => {
    if (typeof window === 'undefined') return '';
    try { return window.sessionStorage.getItem('feral.twin.draft') || ''; }
    catch { return ''; }
  });
  const [twinA, setTwinA] = useState(() => {
    if (typeof window === 'undefined') return null;
    try {
      const stored = window.sessionStorage.getItem('feral.twin.answer');
      return stored || null;
    } catch { return null; }
  });
  const [twinBusy, setTwinBusy] = useState(false);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    try { window.sessionStorage.setItem('feral.twin.draft', twinQ); }
    catch { /* quota / privacy mode — ignore */ }
  }, [twinQ]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    try {
      if (twinA == null) window.sessionStorage.removeItem('feral.twin.answer');
      else window.sessionStorage.setItem('feral.twin.answer', twinA);
    } catch { /* ignore */ }
  }, [twinA]);

  const proactive = useBrainEvents({
    types: [EVENT_TYPES.PROACTIVE, EVENT_TYPES.STATE_PUSH],
    limit: 1,
  });

  const [jobs, setJobs] = useState([]);
  const [jobCounts, setJobCounts] = useState({});
  // Phase-1 truthfulness: track the outcome of the most recent
  // /api/dashboard poll so the hero "Brain" stat can render real
  // state instead of the prior hardcoded `live + pulse` literal.
  // `dashboardError` is `null` on the success branch and a string on
  // the failure branch; `lastDashboardAt` lets future surfaces show
  // "as of N seconds ago" if the operator wants finer-grained truth.
  const [dashboardError, setDashboardError] = useState(null);
  const [lastDashboardAt, setLastDashboardAt] = useState(null);
  // /health probe outcome — third independent signal for the Brain
  // hero stat. `null` until the first poll completes; string on
  // failure; "ok" on success.
  const [healthError, setHealthError] = useState(null);
  const [healthOk, setHealthOk] = useState(false);

  const wsConn = useConnectionStatus();
  const socket = useFeralSocket();
  // Live sub-device summary mirror of dashboard.subdevices_total /
  // subdevices_live. Updated in real time by `subdevice_update` /
  // `subdevice_remove` WS events so the Subdevices tile flips off
  // its pulsing dot within ~1s of a glasses BLE drop instead of
  // waiting for the 15s /api/dashboard poll. Initial seed comes
  // from the /api/dashboard response and is then maintained by the
  // WS events. Keeping a separate state from `dashboard` avoids
  // racing the polled snapshot back over a fresher delta.
  // `fromSocket` records whether the current counts came from a live
  // WS delta rather than from the polled payload. It is what lets the
  // Subdevices tile keep its live dot when the /api/dashboard poll is
  // failing but the socket is still delivering frames, and drop it
  // when both are frozen.
  const [subdevices, setSubdevices] = useState({
    total: 0,
    live: 0,
    rows: new Map(),
    fromSocket: false,
  });

  const refresh = useCallback(async () => {
    // Kick the shared dashboard store on every refresh tick. Fire-and-
    // forget: we never await this and never read sysHealth.* in this
    // closure, which is what kept the previous draft's useCallback
    // identity stable across ticks (a sysHealth dep here would loop
    // with the useEffect below that schedules `refresh`). The store's
    // own tick still drives the dashboard-derived state via the
    // separate `useEffect([sysHealth.data, ...])` further down.
    refreshSystemHealth();
    const results = await Promise.allSettled([
      apiJson('/skills'),
      apiJson('/api/llm/status'),
      apiJson('/api/jobs?limit=10'),
      apiJson('/api/channels'),
      apiJson('/api/ambient/briefing'),
      apiJson('/api/ambient/next_event'),
      apiJson('/api/ambient/wind_down'),
      apiJson('/api/ambient/snapshot'),
      // /health is a separate, cheap probe so the hero "Brain" stat
      // has independent signals (WS open + dashboard ok via store +
      // /health ok). /health responds even when the heavier
      // /api/dashboard composite path is wedged on a sub-system.
      apiJson('/health'),
    ]);
    const [s, l, j, c, b, n, w, snap, healthRes] = results;
    if (s.status === 'fulfilled') setSkills(s.value?.skills || (Array.isArray(s.value) ? s.value : []));
    if (l.status === 'fulfilled') setLlm(l.value);
    if (j.status === 'fulfilled') {
      const items = j.value?.items || [];
      setJobs(items);
      setJobCounts(j.value?.counts_by_kind || {});
      // Back-compat: keep `flows` populated for the legacy TaskFlow
      // widget so anything downstream that reads it still works.
      setFlows(items.filter((it) => it.kind === 'taskflow'));
    }
    if (c.status === 'fulfilled') setChannels(channelMap(c.value));
    if (b.status === 'fulfilled') setBriefing(b.value);
    if (n.status === 'fulfilled') setNextEvent(n.value);
    if (w.status === 'fulfilled') setWindDown(w.value);
    if (snap.status === 'fulfilled') {
      setSnapshot(snap.value);
      // Only steer the tab while the operator has not chosen one. See
      // `modePinnedRef` above.
      if (snap.value?.suggested_mode && !modePinnedRef.current) {
        setMode(snap.value.suggested_mode);
      }
    }
    if (healthRes.status === 'fulfilled') {
      // /health returns `{ "status": "ok", ... }`; anything else is
      // an unhealthy response and we treat the brain as not-fully-up.
      const ok = (healthRes.value?.status === 'ok');
      setHealthOk(ok);
      setHealthError(ok ? null : `unhealthy: ${JSON.stringify(healthRes.value).slice(0, 80)}`);
    } else {
      setHealthOk(false);
      setHealthError(healthRes.reason?.message || 'health probe failed');
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(() => setTime(new Date()), 1000);
    const r = setInterval(refresh, 15000);
    return () => { clearInterval(t); clearInterval(r); };
  }, [refresh]);

  // AUDIT-r14 finding 03: the dashboard payload is owned by the shared
  // useSystemHealth store. Mirror its current snapshot into the legacy
  // local `dashboard` state, derive the sub-device seed, and update the
  // truthful dashboard-error / last-fetched-at signals here so the
  // `refresh` callback above can stay free of sysHealth.* deps (which
  // would otherwise create a useCallback identity loop with the
  // useEffect that schedules `refresh`).
  //
  // Read the ERROR FIRST. `useSystemHealth` deliberately retains the
  // last good payload on a failed poll (hooks/useSystemHealth.js,
  // the catch in `tick`: `snapshot = { data: snapshot.data, ...,
  // error: err }`). That is the right call: a 15s blip should not
  // blank the page. But it means `sysHealth.data` is truthy forever
  // after the first success, so the previous
  // `if (data) { clear error } else if (error) { set error }` ladder
  // could never reach its error branch again. `dashboardError` was
  // pinned to null, `dashboardOk` was pinned to true, and the
  // three-signal offline contract below could not evaluate to
  // `offline` no matter what the brain did. A stopped brain read as
  // "reconnecting…" forever.
  //
  // Retaining stale data is reasonable. Presenting it as live is not,
  // so we keep the payload, mark it stale, and let every derived
  // renderer downgrade off `dashboardStale`.
  useEffect(() => {
    if (sysHealth.error) {
      setDashboardError(
        sysHealth.error?.message
        || sysHealth.error?.detail
        || 'dashboard fetch failed',
      );
      // Deliberately do NOT re-seed `subdevices` here. The payload is
      // unchanged (same object, same `lastFetched`) so re-seeding
      // would only clobber fresher WS deltas with the frozen snapshot.
      return;
    }
    setDashboardError(null);
    if (sysHealth.data) {
      setDashboard(sysHealth.data);
      setLastDashboardAt(sysHealth.lastFetched || Date.now());
      const seedRows = new Map();
      let seedLive = 0;
      for (const dev of (sysHealth.data?.devices || [])) {
        for (const sd of (dev?.subdevices || [])) {
          const key = `${sd.node_id || dev.node_id}:${sd.capability}`;
          seedRows.set(key, { ...sd, node_id: sd.node_id || dev.node_id });
          if (sd.live) seedLive += 1;
        }
      }
      setSubdevices({
        total: sysHealth.data?.subdevices_total ?? seedRows.size,
        live: sysHealth.data?.subdevices_live ?? seedLive,
        rows: seedRows,
        // Reset on every fresh poll: these rows came from the polled
        // payload, not from a live WS frame.
        fromSocket: false,
      });
    }
  }, [sysHealth.data, sysHealth.error, sysHealth.lastFetched]);

  // Real-time sub-device deltas. Without this, the Subdevices tile
  // only refreshes on the 15s poll and a glasses BLE drop looks
  // alive for up to a quarter-minute — that's a status lie a
  // careful demo viewer can spot.
  useEffect(() => {
    const unsub = socket.subscribe((msg) => {
      if (!msg || msg.type !== 'state_push') return;
      const evt = msg.event;
      const data = msg.data;
      if (!data || typeof data !== 'object') return;
      if (evt !== 'subdevice_update' && evt !== 'subdevice_remove') return;
      setSubdevices((prev) => {
        const rows = new Map(prev.rows);
        const key = `${data.node_id}:${data.capability}`;
        if (evt === 'subdevice_update') {
          rows.set(key, data);
        } else {
          rows.delete(key);
        }
        let live = 0;
        for (const r of rows.values()) {
          if (r.live) live += 1;
        }
        return { total: rows.size, live, rows, fromSocket: true };
      });
    });
    return unsub;
  }, [socket]);

  useEffect(() => {
    const onPinChange = () => setPinned(readPinned());
    window.addEventListener('feral_pinned_change', onPinChange);
    window.addEventListener('storage', onPinChange);
    return () => {
      window.removeEventListener('feral_pinned_change', onPinChange);
      window.removeEventListener('storage', onPinChange);
    };
  }, []);

  const askTwin = async (e) => {
    e.preventDefault();
    if (!twinQ.trim()) return;
    setTwinBusy(true);
    setTwinA(null);
    // v2026.5.29 — 30 s client timeout so a hung LLM (or an
    // uninitialised digital_twin in the brain) doesn't make the tile
    // look like a dead button.
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 30_000);
    try {
      const r = await apiFetch(
        `/api/digital-twin/ask?question=${encodeURIComponent(twinQ)}`,
        { signal: controller.signal },
      );
      if (r.ok) {
        const data = await r.json();
        // v2026.5.29 — when the brain returns `{answer:"", error:"..."}`
        // (e.g. DigitalTwin not initialised, optional boot failure),
        // the previous code rendered nothing because `{twinA && ...}`
        // hides empty strings. Surface the error so the operator sees
        // *why* the twin didn't answer.
        const answer = data.answer || data.response || data.reply || '';
        if (answer) {
          setTwinA(answer);
        } else if (data.error) {
          setTwinA(`Twin unavailable: ${data.error}`);
        } else {
          setTwinA('No response.');
        }
      } else {
        setTwinA(`Brain returned ${r.status}.`);
      }
    } catch (err) {
      if (err && err.name === 'AbortError') {
        setTwinA('Timed out waiting for the digital twin. Try again.');
      } else {
        setTwinA(err.message || 'Network error.');
      }
    } finally {
      clearTimeout(timeoutId);
      setTwinBusy(false);
    }
  };

  // One derivation, shared with GlassBrain and CommandPalette. See the
  // doc comment on `deviceCounts` in components/DeviceTopology.jsx for
  // why `device_count`/`online_count` are the same live-only number
  // and why the honest total is `online + paired_offline`. The three
  // surfaces used to each carry their own fallback chain and
  // disagreed on screen.
  const counts = deviceCounts(dashboard);
  const onlineCount = counts.online ?? 0;
  const pairedCount = counts.total ?? 0;
  const pairedOfflineCount = counts.offline ?? 0;
  const skillCount = dashboard?.skills_count ?? skills.length;
  // `/api/dashboard.health` is the brain's `latest_health` dict, and it
  // encodes freshness in the KEY, not in the value: a sample inside the
  // 120s live window lands on `heart_rate`, an older one lands on
  // `heart_rate_stale`, and `heart_rate_fresh` says which happened
  // (api/routes/dashboard.py:326-348).
  //
  // This tile read only `heart_rate`, so on a stale reading it fell
  // through to `somatic.heartRate`, which is `dashboard.somatic
  // .heart_rate`, the SomaticVector's own bpm, carrying no freshness
  // gate at all. The number then rendered bare, with the `via <source>`
  // attribution underneath it (that field IS set on the stale branch),
  // so a forty-minute-old wristband sample was presented exactly like a
  // live one. DeviceTopology's `liveBadges` on the same payload already
  // refuses to badge an unfresh reading; the two surfaces disagreed.
  const health = dashboard?.health || {};
  const hrReported = health.heart_rate ?? health.heart_rate_stale ?? null;
  const hr = Math.round(hrReported ?? somatic.heartRate ?? 0);
  // Stale only when the brain reported a value AND told us it is not
  // fresh. An absent `heart_rate_fresh` (older brain build) is not
  // evidence of staleness, so we do not claim it.
  const hrStale = hrReported != null && health.heart_rate_fresh === false;
  // Surface the wearable source under the bpm so the demo viewer can
  // tell at a glance whether the live tile is reading from the W300
  // glasses, the Veepoo wristband, or a HealthKit mirror. Only shown
  // alongside a value the brain itself reported: attributing the
  // SomaticVector fallback to a named wearable would be a claim we
  // cannot back.
  const hrSource = hrReported != null
    ? (health.heart_rate_source || '').trim()
    : '';
  // `cognitive_load` is NOT a key of `latest_health`. Nothing in
  // api/routes/dashboard.py ever writes one there. The brain ships it
  // on `dashboard.somatic.cognitive_load` (dashboard.py:372), which is
  // exactly what `useSomaticHealth` reads. The old
  // `dashboard?.health?.cognitive_load ?? somatic.cognitiveLoad` was a
  // read of a field that does not exist, silently saved by its own
  // fallback. Read the real field.
  const cog = Math.round(
    ((dashboard?.somatic?.cognitive_load ?? somatic.cognitiveLoad) || 0) * 100,
  );
  const sessionCount = dashboard?.session_count ?? 0;
  // Read from the live mirror first (real-time WS deltas) and fall
  // back to the polled dashboard payload only if the WS hasn't
  // delivered a frame yet. Once the first delta lands the mirror is
  // canonical — the polled snapshot would otherwise race over a
  // fresher value.
  const subdevicesLive = subdevices.rows.size > 0
    ? subdevices.live
    : (dashboard?.subdevices_live ?? 0);
  const subdevicesTotal = subdevices.rows.size > 0
    ? subdevices.total
    : (dashboard?.subdevices_total ?? 0);
  const subdevicesUnavailable = dashboard?.subdevices_unavailable ?? null;
  const alert = proactive?.[0]?.msg?.data || proactive?.[0]?.msg?.payload;

  // Every number on this page below the hero is derived from
  // `dashboard`, and `dashboard` survives a failed poll. When it is
  // stale, the numbers stay (they are the last thing we actually
  // knew) but nothing may claim liveness from them: no green dot, no
  // pulse. The user gets the last-known counts plus a timestamp, and
  // decides for themselves whether a 40-minute-old reading is useful.
  const dashboardStale = dashboard != null && dashboardError != null;
  // A live dot on the Devices tile requires BOTH a device that is
  // online and a reading recent enough to back the claim.
  const devicesLiveNow = onlineCount > 0 && !dashboardStale;
  // Sub-devices are maintained by WS deltas independently of the
  // /api/dashboard poll, so a failing poll does not automatically
  // make them stale. Only a poll failure with no socket frame since
  // does.
  const subdevicesStale = dashboardStale && !subdevices.fromSocket;
  const subdevicesLiveNow = subdevicesLive > 0 && !subdevicesStale;
  const asOfText = lastDashboardAt
    ? new Date(lastDashboardAt).toLocaleTimeString('en-US', {
      hour: 'numeric', minute: '2-digit', second: '2-digit',
    })
    : '';

  // Phase-1 brain liveness: the hero stat is a real binding now, not
  // a hardcoded `live + pulse` literal. Three states map to three
  // user-visible strings, and every dot tone ties to a measurable
  // signal: the WS state, the /health probe, and the
  // /api/dashboard composite. The Brain stat reads `online` only
  // when ALL three agree.
  //
  //   * `online`        — WS open + /health ok + /api/dashboard ok.
  //   * `reconnecting`  — at least one signal is down but at least
  //                       one is still up (transient hiccup, brain
  //                       restart, Tailscale flap).
  //   * `offline`       — WS closed AND /health failed AND
  //                       /api/dashboard failed. Brain is
  //                       unreachable; user needs to act.
  //
  // The previous hardcoded card claimed "online" even when the brain
  // process was stopped on a fresh shell, which is the exact lie
  // the truthfulness audit flagged.
  const wsState = wsConn.state;
  const wsOpen = wsState === 'open';
  const dashboardOk = dashboard != null && dashboardError == null;
  // /health is the strongest "brain process alive" probe — it
  // responds even when the heavier composite path is wedged.
  const httpOk = healthOk && healthError == null;
  let brainTone = 'off';
  let brainLabel = 'offline';
  let brainPulse = false;
  if (wsOpen && httpOk && dashboardOk) {
    brainTone = 'live';
    brainLabel = 'online';
    brainPulse = true;
  } else if (!wsOpen && !httpOk && !dashboardOk) {
    brainTone = 'off';
    brainLabel = 'offline';
  } else {
    // At least one signal is healthy but not all three — surface
    // the partial-degrade state instead of pretending everything
    // is fine. UI text matches the original Phase-1 spec.
    brainTone = 'warn';
    brainLabel = 'reconnecting…';
  }

  const skillsById = new Map(skills.map((s) => [s.skill_id || s.id, s]));
  const pinnedSkills = pinned
    .map((id) => skillsById.get(id))
    .filter(Boolean);
  // Fill up to MAX_PINNED with remaining skills so users always see a row.
  while (pinnedSkills.length < Math.min(MAX_PINNED, skills.length)) {
    const extra = skills.find((s) => !pinnedSkills.includes(s));
    if (!extra) break;
    pinnedSkills.push(extra);
  }
  const overflow = Math.max(skills.length - pinnedSkills.length, 0);

  const nonZeroJobCounts = Object.entries(jobCounts)
    .filter(([, count]) => Number(count) > 0);

  const weather = briefing?.weather;
  const Weather = weather && (WEATHER_ICON[weather.condition] || Sun);
  const hasBriefingContent = Boolean(
    briefing?.sleep
    || weather
    || briefing?.agenda?.length > 0
    || briefing?.goals?.length > 0,
  );
  // Only the sections this page actually renders. The brain also reports
  // `vip_emails:not_implemented` on every single request (EmailWatcher
  // has no VIP recall anywhere in the tree, ambient.py:134-137), and
  // Home renders no VIP mail section, so echoing it here would put a
  // permanent red chip on every install for a pane that does not exist.
  const BRIEFING_SECTIONS = ['sleep', 'agenda', 'goals', 'weather'];
  const briefingDegraded = (Array.isArray(briefing?.degraded) ? briefing.degraded : [])
    .filter((d) => BRIEFING_SECTIONS.includes(String(d).split(':')[0]));
  // Same contract for wind-down: `/api/ambient/wind_down` names the
  // sections whose lookup raised, and an evening whose recap lookup
  // failed must not read as "you finished nothing today".
  const windDownDegraded = (Array.isArray(windDown?.degraded) ? windDown.degraded : [])
    .filter(Boolean);

  return (
    <div className="v2-page v2-page--stack v2-home" data-testid="v2-marker">
      <Pane
        className="v2-home-hero"
        actions={(
          <div className="v2-home-mode-tabs">
            {MODES.map(({ id, label, Icon }) => (
              <button
                key={id}
                type="button"
                onClick={() => pickMode(id)}
                className={`v2-tab${mode === id ? ' is-active' : ''}`}
                aria-pressed={mode === id}
              >
                <Icon size={12} aria-hidden="true" />
                <span className="v2-tab-label">{label}</span>
              </button>
            ))}
            <button type="button" className="v2-btn v2-btn--ghost" onClick={refresh} aria-label="Refresh">
              <RefreshCw size={13} />
            </button>
          </div>
        )}
      >
        <div className="v2-home-hero-body">
          <div className="v2-home-hero-left">
            <Orb size={120} mode={somatic.orbMode || 'idle'} />
            <div>
              <div className="v2-home-greeting">{briefing?.greeting || 'Welcome back'}</div>
              <div className="v2-home-time">
                {time.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true })}
                {' · '}
                {time.toLocaleDateString('en-US', { weekday: 'long', month: 'short', day: 'numeric' })}
              </div>
              {nextEvent?.event ? (
                <div className="v2-home-next">
                  <Sparkles size={12} aria-hidden="true" />
                  Next: <strong>{nextEvent.event.title || nextEvent.event.summary}</strong>
                </div>
              ) : nextEvent?.degraded ? (
                // `/api/ambient/next_event` answers `{event: null,
                // degraded: "<Type>: <msg>"}` when the calendar lookup
                // RAISED, and `{event: null, hint: ...}` when there is
                // simply no calendar. Both used to render as nothing at
                // all, so a calendar that is connected and erroring
                // looked exactly like a quiet day.
                <div
                  className="v2-home-next"
                  data-testid="v2-home-next-event-degraded"
                  style={{ color: 'var(--v2-state-warn)' }}
                  title={nextEvent.hint || undefined}
                >
                  <Sparkles size={12} aria-hidden="true" />
                  Calendar unavailable: {nextEvent.degraded}
                </div>
              ) : null}
            </div>
          </div>

          <div className="v2-home-stats">
            <Glass level={0} radius="md" padding="sm">
              <div className="v2-stat-label">Brain</div>
              <div className="v2-stat-value" data-testid="v2-home-brain-stat">
                <StatusDot tone={brainTone} pulse={brainPulse} label={`Brain ${brainLabel}`} /> {brainLabel}
              </div>
            </Glass>
            <Glass level={0} radius="md" padding="sm"><div className="v2-stat-label">Skills</div>
              <div className="v2-stat-value">{skillCount}</div>
            </Glass>
            <Glass level={0} radius="md" padding="sm"><div className="v2-stat-label">Sessions</div>
              <div className="v2-stat-value">{sessionCount}</div>
            </Glass>
            <Glass level={0} radius="md" padding="sm">
              <div className="v2-stat-label">Devices</div>
              {pairedCount === 0 ? (
                <div className="v2-stat-value" data-testid="v2-home-devices-stat">0</div>
              ) : onlineCount === pairedCount ? (
                // Phase-1 truthfulness contract (operator follow-up
                // on PR #80): bind tone + pulse to a measurable
                // `onlineCount > 0` instead of the literal
                // `tone="live" pulse`. The outer branches today
                // guarantee onlineCount > 0 here, but a future
                // refactor that loosens the invariant would re-
                // introduce the same dot-lie pattern as the Brain
                // hero stat fix. Same defence-in-depth.
                //
                // `devicesLiveNow` adds the second half of that
                // contract: the count must also be CURRENT. With a
                // failing /api/dashboard poll the retained payload
                // still says N devices are online, and painting a
                // pulsing green dot from a frozen cache is a
                // pulsing green dot on a dead brain.
                <div
                  className="v2-stat-value"
                  data-testid="v2-home-devices-stat"
                  title={dashboardStale ? `Last known count, as of ${asOfText}` : undefined}
                >
                  <StatusDot
                    tone={devicesLiveNow ? 'live' : dashboardStale ? 'warn' : 'off'}
                    pulse={devicesLiveNow}
                    label={devicesLiveNow
                      ? `${onlineCount} devices online now`
                      : dashboardStale
                        ? `Device count stale, last known as of ${asOfText}`
                        : 'No devices online'}
                  /> {onlineCount}
                </div>
              ) : (
                // Show online / total when they differ, so the home
                // card is consistent with the "1 paired device —
                // currently offline" banner that already lived below.
                // Previously the card just showed the online count
                // (often 0) and the user saw "0" up top while the
                // banner said "1 paired" — confusing inconsistency.
                <div
                  className="v2-stat-value"
                  data-testid="v2-home-devices-stat"
                  title={dashboardStale
                    ? `Last known count, as of ${asOfText}`
                    : `${pairedOfflineCount} paired but offline`}
                >
                  <StatusDot
                    tone={devicesLiveNow ? 'live' : dashboardStale ? 'warn' : 'neutral'}
                    pulse={devicesLiveNow}
                    label={devicesLiveNow
                      ? `${onlineCount} of ${pairedCount} devices online now`
                      : dashboardStale
                        ? `Device count stale, last known as of ${asOfText}`
                        : `${pairedOfflineCount} paired but offline`}
                  /> {onlineCount}/{pairedCount}
                </div>
              )}
            </Glass>
            {(subdevicesTotal > 0 || subdevicesUnavailable) && (
              // Sub-device tile renders when the brain has ever seen
              // one OR when the truth store can't be read (so the
              // user gets a real warning instead of an empty tile).
              // The dot tone is bound to the live count straight
              // from the brain's truth store; we never invent a
              // pulsing dot when zero subdevices are inside their
              // heartbeat window.
              <Glass level={0} radius="md" padding="sm">
                <div className="v2-stat-label">Subdevices</div>
                <div
                  className="v2-stat-value"
                  data-testid="v2-home-subdevices-stat"
                  title={
                    subdevicesUnavailable
                      ? `Sub-device data temporarily unavailable: ${subdevicesUnavailable}`
                      : `${subdevicesLive} live · ${subdevicesTotal - subdevicesLive} stale`
                  }
                >
                  {subdevicesUnavailable ? (
                    <>
                      <StatusDot tone="warn" label="Sub-device data unavailable" /> unavailable
                    </>
                  ) : (
                    <>
                      <StatusDot
                        tone={subdevicesLiveNow ? 'live' : subdevicesStale ? 'warn' : 'off'}
                        pulse={subdevicesLiveNow}
                        label={subdevicesStale
                          ? `${subdevicesLive} of ${subdevicesTotal} sub-devices live as of ${asOfText}, not current`
                          : `${subdevicesLive} of ${subdevicesTotal} sub-devices live`}
                      /> {subdevicesLive}/{subdevicesTotal}
                    </>
                  )}
                </div>
              </Glass>
            )}
            <Glass level={0} radius="md" padding="sm"><div className="v2-stat-label">Heart rate</div>
              <div
                className="v2-stat-value"
                data-testid="v2-home-hr-stat"
                title={hrStale ? 'Last known reading. The wearable sample is older than the 120s live window.' : undefined}
              >
                {hr > 0 && hrStale ? (
                  <StatusDot tone="warn" label={`Heart rate ${hr}, last known, not current`} />
                ) : null}
                {hr > 0 ? ` ${hr}` : '—'}
              </div>
              {hr > 0 && (hrSource || hrStale) ? (
                <div
                  className="v2-stat-sub"
                  data-testid="v2-home-hr-sub"
                  style={{
                    fontSize: '0.7em',
                    opacity: 0.6,
                    marginTop: '0.15em',
                    color: hrStale ? 'var(--v2-state-warn)' : undefined,
                  }}
                >
                  {hrStale ? 'last known' : null}
                  {hrStale && hrSource ? ' · ' : null}
                  {hrSource ? `via ${hrSource}` : null}
                </div>
              ) : null}
            </Glass>
            <Glass level={0} radius="md" padding="sm"><div className="v2-stat-label">Load</div>
              <div className="v2-stat-value">{cog}%</div>
            </Glass>
          </div>
        </div>

        {/* Staleness stamp. `lastDashboardAt` was computed and then
            never rendered, so a page frozen on a cached payload gave
            the user no way to tell how old it was. Same "As of
            HH:MM:SS" shape as components/ConnectedHardware.jsx, plus
            the reason when the poll is currently failing. */}
        {asOfText && (
          <div
            className="v2-p v2-p--tiny v2-p--muted"
            style={{
              marginTop: 8,
              color: dashboardStale ? 'var(--v2-state-warn)' : 'var(--v2-text-tertiary)',
            }}
            data-testid="v2-home-dashboard-stamp"
            title={dashboardStale ? dashboardError : undefined}
          >
            {dashboardStale
              ? `Stale. Last read from the brain at ${asOfText}. ${dashboardError}`
              : `As of ${asOfText}`}
          </div>
        )}
      </Pane>

      {alert && (alert.title || alert.message) && (
        <Glass level={1} radius="md" padding="md" className="v2-dash-alert">
          <Sparkles size={14} aria-hidden="true" />
          <div>
            <div className="v2-dash-alert-title">{alert.title || 'Heads up'}</div>
            <div className="v2-dash-alert-msg">{alert.message || ''}</div>
          </div>
        </Glass>
      )}

      {pairedCount === 0 ? (
        <Glass level={1} radius="lg" padding="md" className="v2-dash-cta">
          <div className="v2-dash-cta-body">
            <Plug size={18} aria-hidden="true" />
            <div>
              <div className="v2-dash-cta-title">No devices paired yet</div>
              <div className="v2-dash-cta-hint">
                Pair a phone browser, wristband, smart glasses, laptop bridge, or any HUP node. FERAL starts reading their sensors the moment they attach.
              </div>
            </div>
            <Link to="/devices" className="v2-btn v2-btn--primary">Pair</Link>
          </div>
        </Glass>
      ) : onlineCount === 0 ? (
        <Glass level={1} radius="lg" padding="md" className="v2-dash-cta">
          <div className="v2-dash-cta-body">
            <Plug size={18} aria-hidden="true" />
            <div>
              <div className="v2-dash-cta-title">
                {pairedCount === 1
                  ? '1 paired device — currently offline'
                  : `${pairedCount} paired devices — none online right now`}
              </div>
              <div className="v2-dash-cta-hint">
                Pairing succeeded. Re-open the device's FERAL app or HUP daemon to bring it back online. The brain will pick the WebSocket session up automatically.
              </div>
            </div>
            <Link to="/devices" className="v2-btn">Manage devices</Link>
          </div>
        </Glass>
      ) : pairedOfflineCount > 0 ? (
        <Glass level={1} radius="lg" padding="sm" className="v2-dash-cta">
          <div className="v2-dash-cta-body">
            <Plug size={16} aria-hidden="true" />
            <div>
              <div className="v2-dash-cta-title">
                {onlineCount} online · {pairedOfflineCount} paired but offline
              </div>
            </div>
            <Link to="/devices" className="v2-btn v2-btn--ghost">View</Link>
          </div>
        </Glass>
      ) : null}

      <Pane title={`Skills (${skillCount})`} actions={(
        <button type="button" className="v2-btn v2-btn--ghost" onClick={() => setLauncherOpen(true)}>
          View all <ChevronRight size={12} />
        </button>
      )}>
        <div className="v2-skill-pinstrip">
          {pinnedSkills.map((s) => {
            const id = s.skill_id || s.id;
            return (
              <button
                key={id}
                type="button"
                className="v2-skill-pin"
                onClick={() => setLauncherOpen(true)}
                title={`${s.name || id} — ${s.description || ''}`}
              >
                <span className="v2-skill-pin-glyph" aria-hidden="true">{SKILL_GLYPH[id] || '•'}</span>
                <span className="v2-skill-pin-name">{s.name || id}</span>
              </button>
            );
          })}
          {overflow > 0 && (
            <button
              type="button"
              className="v2-skill-pin v2-skill-pin--more"
              onClick={() => setLauncherOpen(true)}
              aria-label={`Open skills launcher — ${overflow} more skills`}
            >
              <Plus size={14} aria-hidden="true" />
              <span className="v2-skill-pin-name">{overflow} more</span>
            </button>
          )}
          {pinnedSkills.length === 0 && overflow === 0 && (
            <EmptyState title="No skills loaded yet" hint="Check the Brain boot log." />
          )}
        </div>
      </Pane>

      {/*
        * Briefing mode. The gate used to be
        *   mode === 'briefing' && (sleep || weather || agenda || goals)
        * so on any brain where all four are empty (which is every fresh
        * install, and every install with no wearable baseline, no
        * calendar and no OPENWEATHER_API_KEY) clicking "Briefing"
        * rendered literally nothing. Measured against a live brain: the
        * page contained zero `.v2-home-grid` nodes in briefing mode, so
        * the tab was visually indistinguishable from a dead button.
        *
        * Worse, `/api/ambient/briefing` reports which of its four
        * sections FAILED in `degraded[]` (api/routes/ambient.py:66),
        * and none of it reached the screen: a briefing whose sleep,
        * agenda and goals lookups all raised looked exactly like a
        * quiet morning.
        */}
      {mode === 'briefing' && (briefingDegraded.length > 0 || !hasBriefingContent) && (
        <Pane title="Briefing">
          {briefingDegraded.length > 0 && (
            <div
              className="v2-chip v2-chip--error"
              data-testid="v2-home-briefing-degraded"
            >
              Briefing incomplete: {briefingDegraded.join(', ')} could not be read.
            </div>
          )}
          {!hasBriefingContent && (
            <EmptyState
              title={briefingDegraded.length > 0
                ? 'Briefing could not be assembled'
                : 'Nothing to brief yet'}
              hint={briefingDegraded.length > 0
                ? 'The sections above failed to load. Check the brain log.'
                : 'Sleep needs a wearable baseline, agenda needs a connected calendar, weather needs OPENWEATHER_API_KEY, and goals need an active intent plan.'}
            />
          )}
        </Pane>
      )}
      {mode === 'briefing' && hasBriefingContent && (
        <div className="v2-home-grid">
          {briefing?.sleep && (
            <Glass level={1} radius="md" padding="md">
              <div className="v2-stat-label">Sleep</div>
              <div className="v2-stat-value">HRV {briefing.sleep.hrv_ms}ms</div>
              <div className="v2-p v2-p--muted">{briefing.sleep.trend}</div>
            </Glass>
          )}
          {weather && Weather && (
            <Glass level={1} radius="md" padding="md">
              <div className="v2-stat-label">Weather</div>
              <div className="v2-stat-value"><Weather size={14} /> {Math.round(weather.temp_c)}°C</div>
              <div className="v2-p v2-p--muted">{weather.description}</div>
              {weather.outfit_hint && <div className="v2-p v2-p--tiny">Wear: {weather.outfit_hint}</div>}
            </Glass>
          )}
          {briefing?.agenda?.length > 0 && (
            <Glass level={1} radius="md" padding="md">
              <div className="v2-stat-label">Agenda</div>
              <ul className="v2-ambient-list">
                {briefing.agenda.slice(0, 3).map((a, i) => (
                  <li key={i}>{a.title || a.action || JSON.stringify(a).slice(0, 120)}</li>
                ))}
              </ul>
            </Glass>
          )}
          {briefing?.goals?.length > 0 && (
            <Glass level={1} radius="md" padding="md">
              <div className="v2-stat-label">Goals</div>
              <ul className="v2-ambient-list">
                {briefing.goals.slice(0, 3).map((g) => (
                  <li key={g.id}>{g.title} · <span className="v2-p v2-p--muted">{Math.round((g.progress || 0) * 100)}%</span></li>
                ))}
              </ul>
            </Glass>
          )}
        </div>
      )}

      {/*
        * AUDIT-r14 finding 03 fix #3 — Desk mode content. The toggle
        * existed in the mode tabs but no `mode === 'desk'` block ever
        * rendered, so clicking Desk did nothing visible. We surface
        * the most useful in-the-moment surfaces for "deep work" — the
        * highest-priority job queue + a live somatic/vitals chip
        * + the most relevant intent for "now".
        */}
      {mode === 'desk' && (
        <div className="v2-home-grid">
          <Glass level={1} radius="md" padding="md">
            <div className="v2-stat-label">In-flight jobs</div>
            {jobs.length === 0 ? (
              <div className="v2-p v2-p--muted">Quiet. Nothing queued.</div>
            ) : (
              <ul className="v2-ambient-list">
                {/*
                  * `/api/jobs` items are
                  * {id, kind, name, status, started_at, progress,
                  *  context_session_id, cancellable_via, detail}:
                  * every one of the six aggregator sources builds that
                  * shape (api/routes/jobs.py). There is no
                  * `description` key anywhere in it, so this row read a
                  * field the brain never sends and dropped the job's
                  * name entirely. Measured against a live brain with
                  * one real routine queued, the row rendered
                  * "routine · scheduled" while "Right now" showed the
                  * same job as "Routine · Audit probe routine for Home
                  * page · scheduled". Same payload, two panes, and only
                  * one of them told you WHICH job.
                  *
                  * `jobKindLabel` for the same reason it exists in
                  * "Right now": the raw kinds are the brain's internal
                  * source names ("tool_genesis", "background_bash").
                  */}
                {jobs.slice(0, 6).map((j) => (
                  <li key={j.id || j.job_id}>
                    <strong>{jobKindLabel(j.kind)}</strong>
                    {j.name || j.description ? ` · ${j.name || j.description}` : ''}
                    {j.status ? ` · ${j.status}` : ''}
                  </li>
                ))}
              </ul>
            )}
          </Glass>
          <Glass level={1} radius="md" padding="md">
            <div className="v2-stat-label">Body</div>
            <div className="v2-stat-value">
              {Math.round((somatic?.heartRate || 0))} bpm
            </div>
            <div className="v2-p v2-p--muted">
              Cognitive load {(somatic?.cognitiveLoad ?? 0).toFixed(2)} · orb {somatic?.orbMode}
            </div>
          </Glass>
          {briefing?.agenda?.length > 0 && (
            <Glass level={1} radius="md" padding="md">
              <div className="v2-stat-label">Next on calendar</div>
              <ul className="v2-ambient-list">
                {briefing.agenda.slice(0, 3).map((evt, i) => (
                  <li key={evt.id || i}>{evt.title || evt.summary}{evt.start ? ` · ${evt.start}` : ''}</li>
                ))}
              </ul>
            </Glass>
          )}
        </div>
      )}

      {mode === 'wind_down' && windDownDegraded.length > 0 && (
        <div
          className="v2-chip v2-chip--error"
          data-testid="v2-home-winddown-degraded"
        >
          Wind-down incomplete: {windDownDegraded.join(', ')} could not be read.
        </div>
      )}

      {mode === 'wind_down' && (windDown?.day_recap?.completed_tasks?.length > 0 || windDown?.sleep_prep || windDown?.journal_prompt) && (
        <div className="v2-home-grid">
          {windDown?.day_recap?.completed_tasks?.length > 0 && (
            <Glass level={1} radius="md" padding="md">
              <div className="v2-stat-label">Completed today</div>
              <ul className="v2-ambient-list">
                {windDown.day_recap.completed_tasks.slice(0, 5).map((t, i) => (
                  <li key={i}>{t.title || t}</li>
                ))}
              </ul>
            </Glass>
          )}
          {windDown?.sleep_prep && (
            <Glass level={1} radius="md" padding="md">
              <div className="v2-stat-label">Sleep prep</div>
              <div className="v2-stat-value">{Math.round((windDown.sleep_prep.time_to_bed_min || 0) / 60)}h</div>
              {windDown.sleep_prep.hints?.length > 0 && (
                <ul className="v2-ambient-list">
                  {windDown.sleep_prep.hints.map((h, i) => <li key={i}>{h}</li>)}
                </ul>
              )}
            </Glass>
          )}
          {windDown?.journal_prompt && (
            <Glass level={1} radius="md" padding="md">
              <div className="v2-stat-label">Journal prompt</div>
              <div className="v2-p">{windDown.journal_prompt}</div>
            </Glass>
          )}
        </div>
      )}

      <ForYouToday />

      <ConnectedHardware />

      <ResumeCockpit />

      <div className="v2-dash-row v2-dash-row--double">
        <Pane title="Channels">
          {Object.keys(channels).length === 0 && (
            <EmptyState
              title="No channels configured"
              hint="Set FERAL_TELEGRAM_BOT_TOKEN etc. in your shell, or open Settings → Channels."
            />
          )}
          <div className="v2-channel-list">
            {Object.entries(channels).map(([name, info]) => {
              const st = channelState(info);
              return (
                <Glass key={name} level={0} radius="sm" padding="sm" className="v2-channel-row">
                  <StatusDot
                    tone={st.tone}
                    pulse={st.tone === 'live'}
                    label={`${name} ${st.label}`}
                  />
                  <span className="v2-channel-name">{name}</span>
                  <span
                    className="v2-channel-state"
                    data-testid={`v2-home-channel-${name}`}
                    title={st.reason || undefined}
                  >
                    {st.label}
                  </span>
                </Glass>
              );
            })}
          </div>
        </Pane>

        <Pane title="LLM">
          {llm ? (
            <div className="v2-setting-stack">
              <div className="v2-setting-row">
                <div className="v2-setting-label"><div>Provider</div></div>
                <div className="v2-setting-control">
                  <StatusDot
                    tone={llm.available ? 'live' : 'warn'}
                    label={`LLM provider ${llm.provider || 'unknown'} ${llm.available ? 'available' : 'unavailable'}`}
                  /> {llm.provider || '—'}
                </div>
              </div>
              <div className="v2-setting-row">
                <div className="v2-setting-label"><div>Model</div></div>
                <div className="v2-setting-control">{llm.model || '—'}</div>
              </div>
              {llm.reason && (
                <div className="v2-setting-row">
                  <div className="v2-setting-label"><div>Reason</div></div>
                  <div className="v2-setting-control v2-p v2-p--muted">{llm.reason}</div>
                </div>
              )}
            </div>
          ) : <EmptyState title="LLM status pending" />}
        </Pane>
      </div>

      <div className="v2-dash-row v2-dash-row--double">
        <Pane
          title={`Right now${jobs.length ? ` · ${jobs.length}` : ''}`}
          actions={(
            <Link to="/flows" className="v2-btn v2-btn--ghost">Manage flows <ChevronRight size={12} /></Link>
          )}
        >
          <p className="v2-p v2-p--muted">
            Everything FERAL is working on: TaskFlows, scheduled routines, specialists on standby, new tools being drafted, background shell jobs, and live devices.
          </p>
          {jobs.length === 0 ? (
            <EmptyState title="Idle" hint="No active jobs. Schedule a routine or start a TaskFlow to see activity here." />
          ) : (
            <div className="v2-flow-mini-list">
              {jobs.map((j) => (
                <Glass key={j.id} level={0} radius="sm" padding="sm" className="v2-flow-row" title={j.detail ? JSON.stringify(j.detail) : ''}>
                  <StatusDot
                    tone={j.status === 'running' ? 'live' : j.status === 'failed' || j.status === 'error' ? 'error' : j.status === 'paused' ? 'warn' : 'neutral'}
                    pulse={j.status === 'running' || j.status === 'connected'}
                    label={`${jobKindLabel(j.kind)} ${j.name || ''}: ${j.status || 'unknown'}`}
                  />
                  <div className="v2-flow-title">
                    <span className="v2-chip v2-chip--muted" style={{ marginRight: 6 }}>{jobKindLabel(j.kind)}</span>
                    {j.name}
                  </div>
                  <div className="v2-flow-status">
                    {j.status}
                    {typeof j.progress === 'number' && ` · ${Math.round(j.progress * 100)}%`}
                  </div>
                </Glass>
              ))}
            </div>
          )}
          {/*
            * `counts_by_kind` always carries all six aggregator sources,
            * zeros included, so this row rendered "TaskFlow: 0 ·
            * Routine: 0 · Specialist: 0 · New tool: 0 · Device: 0 ·
            * Shell job: 0" directly beneath the "Idle, no active jobs"
            * empty state on every quiet brain. Six chips restating what
            * the empty state just said. Show only the kinds that have
            * something in them.
            */}
          {nonZeroJobCounts.length > 0 && (
            <div className="v2-device-caps" style={{ marginTop: 10 }}>
              {nonZeroJobCounts.map(([kind, count]) => (
                <span key={kind} className="v2-chip v2-chip--muted">{jobKindLabel(kind)}: {count}</span>
              ))}
            </div>
          )}
        </Pane>

        <Pane title="Ask your Digital Twin">
          <form onSubmit={askTwin} className="v2-twin-form">
            <input
              className="v2-input v2-twin-input"
              value={twinQ}
              onChange={(e) => setTwinQ(e.target.value)}
              placeholder="What do I usually do on Sunday evenings?"
              disabled={twinBusy}
            />
            <button type="submit" className="v2-btn v2-btn--primary" disabled={twinBusy || !twinQ.trim()}>
              {twinBusy ? 'Thinking…' : 'Ask'}
            </button>
          </form>
          {twinA && <div className="v2-twin-answer">{twinA}</div>}
        </Pane>
      </div>

      <SkillsLauncher open={launcherOpen} onClose={() => setLauncherOpen(false)} skills={skills} />
    </div>
  );
}
