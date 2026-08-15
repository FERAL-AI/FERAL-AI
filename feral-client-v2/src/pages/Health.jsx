import React, { useState } from 'react';
import { Heart, AlertTriangle, TrendingUp, Activity } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Tabs from '../ui/Tabs';
import EmptyState from '../ui/EmptyState';
import ErrorState from '../ui/ErrorState';
import StatusDot from '../ui/StatusDot';
import { useResource } from '../hooks/useResource';

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
  // Was: `.catch(() => setS({}))`, which rendered "Metrics tracked 0 /
  // Recent alerts 0 / Categories 0" whenever the request failed. Three
  // zeros are a claim about the user's health baseline; a failed fetch
  // is not entitled to make it.
  const { data: s, error, refresh } = useResource('/api/baseline/summary');
  if (error && !s) {
    return (
      <Pane title="Summary">
        <ErrorState error={error} what="the baseline summary" onRetry={refresh} />
      </Pane>
    );
  }
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
  // Was: `.then(...).finally(...)` with no catch at all, so a rejected
  // promise left `metrics` at its initial `[]` and the pane asserted
  // "No metrics yet".
  const { data, error, loading, refresh } = useResource('/api/baseline/metrics', {
    select: (d) => (Array.isArray(d?.metrics) ? d.metrics : (Array.isArray(d) ? d : [])),
  });
  const metrics = data || [];
  return (
    // No count in the title until we actually have a list. "(0)" next
    // to a failed fetch reads as a measurement.
    <Pane title={data ? `Metrics (${metrics.length})` : 'Metrics'}>
      {loading && !data && <EmptyState title="Loading…" />}
      {error && !data && (
        <ErrorState error={error} what="baseline metrics" onRetry={refresh} />
      )}
      {!loading && !error && data && metrics.length === 0 && <EmptyState title="No metrics yet" hint="Pair a wristband or phone to start populating baselines." />}
      <div className="v2-skills-grid">
        {metrics.map((m, i) => {
          // Backend (`/api/baseline/metrics`) returns BaselineMetric rows:
          // { metric_id, category, values: [...], mean, std_dev }. The
          // legacy field names (metric/value/unit/samples) never existed,
          // which is why the cards rendered blank. Read the real fields,
          // keeping the old names as fallbacks for any shimmed payload.
          const name = m.metric_id || m.metric || m.name || 'metric';
          const mean = typeof m.mean === 'number' ? m.mean : m.value;
          const sampleCount = Array.isArray(m.values) ? m.values.length : m.samples;
          return (
            <Glass key={name + i} level={0} radius="md" padding="md">
              <header className="v2-skill-card-head">
                <h3 className="v2-skill-card-name">{name}</h3>
                {(m.category || m.unit) && (
                  <code className="v2-skill-card-id">{m.category || m.unit}</code>
                )}
              </header>
              <div className="v2-stat-value">
                {typeof mean === 'number' ? mean.toFixed(1) : (mean ?? '—')}
              </div>
              <div className="v2-skill-card-meta">
                {typeof m.std_dev === 'number' && m.std_dev > 0 && (
                  <span className="v2-chip"><TrendingUp size={10} /> σ {m.std_dev.toFixed(1)}</span>
                )}
                {sampleCount ? <span className="v2-chip">{sampleCount} samples</span> : null}
              </div>
            </Glass>
          );
        })}
      </div>
    </Pane>
  );
}

function AlertsTab() {
  // The worst instance of the bug class: no catch at all, so any
  // failure left `alerts` at `[]` and this pane rendered "No anomalies
  // detected" over a health surface. Silence is now silence, not an
  // all-clear.
  const { data, error, loading, refresh } = useResource('/api/baseline/alerts', {
    select: (d) => (Array.isArray(d?.alerts) ? d.alerts : (Array.isArray(d) ? d : [])),
  });
  const alerts = data || [];

  // Two bugs here before this change. The expression was
  //   sev === 'high'||'critical' ? 'error' : 'medium' ? 'warn' : 'live'
  // which by JS precedence is `(sev === 'high') || 'critical'`, always
  // truthy, so every alert painted as 'error'; and the unreachable
  // fallback was 'live', the same green that means "healthy" on every
  // other surface. A low-severity anomaly is neither an emergency nor
  // an all-clear, so it gets the neutral dot.
  const tone = (sev) => {
    const s = String(sev || '').toLowerCase();
    if (s === 'high' || s === 'critical') return 'error';
    if (s === 'medium') return 'warn';
    return 'neutral';
  };

  return (
    <Pane title={data ? `Alerts (${alerts.length})` : 'Alerts'}>
      {loading && !data && <EmptyState title="Loading…" />}
      {error && !data && (
        <ErrorState error={error} what="health alerts" onRetry={refresh} />
      )}
      {!loading && !error && data && alerts.length === 0 && <EmptyState title="No anomalies detected" />}
      <ul className="v2-mem-list">
        {alerts.map((a, i) => (
          <li key={a.id || i}>
            <Glass level={0} radius="md" padding="md">
              <div className="v2-flow-card-head">
                <StatusDot
                  tone={tone(a.severity)}
                  label={`${a.metric || a.title || a.id}: severity ${a.severity || 'unspecified'}`}
                />
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

// AUDIT-r14 finding 06 fix for Health/Today: backend at
// api/routes/timeline.py:154-163 returns `{data: {...vitals}, error?}`.
// The page used to set `today` to the whole response, so the metric
// tiles rendered "data" / "error" as keys instead of the actual vital
// names. Unpack `.data` (with a back-compat fall-through if a future
// build flattens the shape).
//
// The old `resp.error` branch is gone: apiFetch (lib/api.js:88-93,
// 144-150) already converts a 200 whose body carries a truthy `error`
// into a thrown ApiError, so that branch could never run and the
// failure landed in the `.catch` that set `{}` and rendered "No vitals
// yet". It is an ErrorState now.
function unpackVitals(resp) {
  if (!resp || typeof resp !== 'object') return {};
  if (resp.data && typeof resp.data === 'object') return resp.data;
  return resp;
}

function TodayTab() {
  const {
    data: today, error: todayError, loading: todayLoading, refresh: refreshToday,
  } = useResource('/api/health-summary', { select: unpackVitals });
  // Pull the dashboard so we can render a real pipeline+source line
  // alongside the metric tiles. The vital values themselves come from
  // the aggregator above; the sources list comes from the brain's
  // sub-device truth store on /api/dashboard so each chip is bound
  // to the same `live` flag the rest of the dashboard uses.
  const {
    data: sourceRows, error: sourcesError, refresh: refreshSources,
  } = useResource('/api/dashboard', {
    select: (d) => {
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
      return out;
    },
  });
  const sources = sourceRows || [];
  if (todayLoading && !today) return <Pane title="Today"><EmptyState title="Loading…" /></Pane>;
  const visibleEntries = today
    ? Object.entries(today).filter(([k]) => !k.startsWith('_'))
    : [];
  return (
    <>
      <Pane title="Today's vitals">
        {todayError && !today && (
          <ErrorState error={todayError} what="today's vitals" onRetry={refreshToday} />
        )}
        {!todayError && today && visibleEntries.length === 0 && (
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
        {sourcesError && !sourceRows ? (
          <ErrorState
            error={sourcesError}
            what="the active source list"
            hint="The brain's sub-device truth store did not answer, so we cannot tell you which pipelines are feeding vitals right now."
            onRetry={refreshSources}
          />
        ) : sourceRows && sources.length === 0 ? (
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
