import React, { useEffect, useState } from 'react';
import { Heart, AlertTriangle, TrendingUp, Activity } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Tabs from '../ui/Tabs';
import EmptyState from '../ui/EmptyState';
import StatusDot from '../ui/StatusDot';
import { apiJson } from '../lib/api';

export default function Health() {
  const [tab, setTab] = useState('summary');
  return (
    <div className="v2-page v2-page--stack" data-testid="v2-marker">
      <Pane
        title="Health baseline"
        actions={(
          <Tabs
            value={tab}
            onChange={setTab}
            items={[
              { id: 'summary', label: 'Summary' },
              { id: 'metrics', label: 'Metrics' },
              { id: 'alerts', label: 'Alerts' },
              { id: 'today', label: 'Today' },
            ]}
          />
        )}
      >
        <p className="v2-p v2-p--muted">
          FERAL's baseline engine watches your HR, sleep, activity, BP, and cognitive
          load. Deviations surface as alerts. Never diagnostic — always informational.
        </p>
      </Pane>
      {tab === 'summary' && <SummaryTab />}
      {tab === 'metrics' && <MetricsTab />}
      {tab === 'alerts' && <AlertsTab />}
      {tab === 'today' && <TodayTab />}
    </div>
  );
}

function SummaryTab() {
  const [s, setS] = useState(null);
  useEffect(() => { apiJson('/api/baseline/summary').then(setS).catch(() => setS({})); }, []);
  if (!s) return <Pane title="Summary"><EmptyState title="Loading…" /></Pane>;
  return (
    <Pane title="Summary">
      <div className="v2-grid v2-grid--stats">
        <Glass level={1} radius="md" padding="md">
          <div className="v2-stat-label">Metrics tracked</div>
          <div className="v2-stat-value">{s.metrics_tracked ?? 0}</div>
        </Glass>
        <Glass level={1} radius="md" padding="md">
          <div className="v2-stat-label">Recent alerts</div>
          <div className="v2-stat-value">{s.recent_alerts ?? 0}</div>
        </Glass>
        <Glass level={1} radius="md" padding="md">
          <div className="v2-stat-label">Categories</div>
          <div className="v2-stat-value">{Array.isArray(s.categories) ? s.categories.length : 0}</div>
        </Glass>
      </div>
      {Array.isArray(s.categories) && s.categories.length > 0 && (
        <div className="v2-skill-card-phrases" style={{ marginTop: 12 }}>
          {s.categories.map((c) => <span key={c} className="v2-chip">{c}</span>)}
        </div>
      )}
    </Pane>
  );
}

function MetricsTab() {
  const [metrics, setMetrics] = useState([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    apiJson('/api/baseline/metrics')
      .then((d) => setMetrics(d.metrics || d || []))
      .finally(() => setLoading(false));
  }, []);
  return (
    <Pane title={`Metrics (${metrics.length})`}>
      {loading && <EmptyState title="Loading…" />}
      {!loading && metrics.length === 0 && <EmptyState title="No metrics yet" hint="Pair a wristband or phone to start populating baselines." />}
      <div className="v2-skills-grid">
        {metrics.map((m, i) => (
          <Glass key={m.metric || i} level={0} radius="md" padding="md">
            <header className="v2-skill-card-head">
              <h3 className="v2-skill-card-name">{m.metric || m.name}</h3>
              {m.unit && <code className="v2-skill-card-id">{m.unit}</code>}
            </header>
            <div className="v2-stat-value">{typeof m.value === 'number' ? m.value.toFixed(1) : m.value}</div>
            <div className="v2-skill-card-meta">
              {m.trend && <span className="v2-chip"><TrendingUp size={10} /> {m.trend}</span>}
              {m.samples && <span className="v2-chip">{m.samples} samples</span>}
            </div>
          </Glass>
        ))}
      </div>
    </Pane>
  );
}

function AlertsTab() {
  const [alerts, setAlerts] = useState([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    apiJson('/api/baseline/alerts')
      .then((d) => setAlerts(d.alerts || d || []))
      .finally(() => setLoading(false));
  }, []);

  const tone = (sev) => sev === 'high' || sev === 'critical' ? 'error' : sev === 'medium' ? 'warn' : 'live';

  return (
    <Pane title={`Alerts (${alerts.length})`}>
      {loading && <EmptyState title="Loading…" />}
      {!loading && alerts.length === 0 && <EmptyState title="No anomalies detected" />}
      <ul className="v2-mem-list">
        {alerts.map((a, i) => (
          <li key={a.id || i}>
            <Glass level={0} radius="md" padding="md">
              <div className="v2-flow-card-head">
                <StatusDot tone={tone(a.severity)} />
                <div className="v2-flow-card-title"><AlertTriangle size={12} /> {a.metric || a.title || a.id}</div>
                <div className="v2-flow-card-status">{a.severity}</div>
              </div>
              <div className="v2-mem-content">{a.message || a.description || JSON.stringify(a).slice(0, 160)}</div>
            </Glass>
          </li>
        ))}
      </ul>
    </Pane>
  );
}

/**
 * Phase-1 truthfulness sweep: explicit pipeline qualifier per
 * vitals source. Maps the brain's capability id (what the iOS
 * adapter / cloud integration declares on `node_register`) to the
 * human-readable pipeline label rendered in the iOS Vitals tab so
 * web + native stay consistent. The mapping mirrors
 * `feral-companion-ios` `HealthStore.defaultPipelineLabel(for:)`.
 */
function pipelineLabelForCapability(cap) {
  switch (cap) {
    case 'apple_healthkit': return 'Apple Health';
    case 'jw_health_glasses': return 'Theora glasses';
    case 'veepoo_wristband': return 'Veepoo wristband';
    case 'w610_glasses': return 'W610 open glasses';
    case 'generic_ble_hr': return 'BLE heart-rate sensor';
    case 'whoop_cloud': return 'Whoop';
    case 'oura_cloud': return 'Oura';
    case 'strava_cloud': return 'Strava';
    case 'garmin_cloud': return 'Garmin';
    case 'fitbit_cloud': return 'Fitbit';
    default: return cap || 'unknown source';
  }
}

function TodayTab() {
  const [today, setToday] = useState(null);
  const [sources, setSources] = useState([]);
  useEffect(() => {
    // AUDIT-r14 finding 06 fix for Health/Today: backend at
    // api/routes/timeline.py:154-163 returns `{data: {...vitals},
    // error?}`. The page used to set `today` to the whole response,
    // so the metric tiles rendered "data" / "error" as keys instead
    // of the actual vital names. Unpack `.data` (with a back-compat
    // fall-through if a future build flattens the shape).
    apiJson('/api/health-summary')
      .then((resp) => {
        if (resp && typeof resp === 'object') {
          if (resp.error) {
            setToday({ _error: resp.error });
          } else if (resp.data && typeof resp.data === 'object') {
            setToday(resp.data);
          } else {
            setToday(resp);
          }
        } else {
          setToday({});
        }
      })
      .catch(() => setToday({}));
    // Pull the dashboard so we can render a real pipeline+source line
    // alongside the metric tiles. The vital values themselves come from
    // the aggregator above; the sources list comes from the brain's
    // sub-device truth store on /api/dashboard so each chip is bound
    // to the same `live` flag the rest of the dashboard uses.
    apiJson('/api/dashboard')
      .then((d) => {
        const out = [];
        for (const dev of (d?.devices || [])) {
          for (const s of (dev?.subdevices || [])) {
            out.push({
              ...s,
              node_id: s.node_id || dev.node_id,
              pipeline: pipelineLabelForCapability(s.capability),
              sample_source: s?.attrs?.device_name || s?.attrs?.sample_source || '',
            });
          }
        }
        setSources(out);
      })
      .catch(() => setSources([]));
  }, []);
  if (!today) return <Pane title="Today"><EmptyState title="Loading…" /></Pane>;
  const errOnly = today._error;
  const visibleEntries = Object.entries(today).filter(([k]) => !k.startsWith('_'));
  return (
    <>
      <Pane title="Today's vitals">
        {errOnly && <div className="v2-chip v2-chip--error" role="alert">{errOnly}</div>}
        {!errOnly && visibleEntries.length === 0 && (
          <EmptyState title="No vitals yet" hint="Pair a wearable or HealthKit-enabled iPhone to populate today's snapshot." />
        )}
        <div className="v2-grid v2-grid--stats">
          {visibleEntries.map(([k, v]) => (
            <Glass key={k} level={1} radius="md" padding="md">
              <div className="v2-stat-label">{k.replace(/_/g, ' ')}</div>
              <div className="v2-stat-value">{typeof v === 'number' ? v : JSON.stringify(v).slice(0, 40)}</div>
            </Glass>
          ))}
        </div>
      </Pane>
      <Pane title={`Active sources${sources.length ? ` · ${sources.length}` : ''}`}>
        {sources.length === 0 ? (
          <EmptyState
            title="No active sources"
            hint="Pair a device or connect a cloud integration. The pipeline label here matches the iOS Vitals tab so you can verify which transport a number came from."
          />
        ) : (
          <div className="v2-skill-card-phrases">
            {sources.map((s, i) => (
              <span
                key={`${s.node_id}-${s.capability}-${i}`}
                className={`v2-chip ${s.live ? 'v2-chip--live' : ''}`}
                data-testid="v2-vitals-source-chip"
                title={[
                  s.live ? 'live' : 'stale',
                  `provenance: ${s.provenance || 'unknown'}`,
                  typeof s.last_seen === 'number'
                    ? `last seen ${Math.round(Math.max(0, Date.now() / 1000 - s.last_seen))} s ago`
                    : null,
                ].filter(Boolean).join('\n')}
              >
                <StatusDot
                  tone={s.live ? 'live' : 'off'}
                  pulse={s.live}
                  label={`${s.pipeline} ${s.live ? 'live' : 'stale'}`}
                />
                {s.pipeline}
                {s.sample_source && <span className="v2-chip-suffix"> · {s.sample_source}</span>}
              </span>
            ))}
          </div>
        )}
      </Pane>
    </>
  );
}
