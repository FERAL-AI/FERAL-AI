import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Brain, Cpu, Activity, Database, MessageSquare, Puzzle,
  Wifi, WifiOff, Zap, Shield, BookOpen, Clock, TrendingUp,
  Sparkles, Heart, Thermometer, Wind, Sun, RefreshCw,
  Search, Bluetooth, Radio, Globe, ChevronRight, Loader2,
} from 'lucide-react';

import { API_BASE as API, WS_URL } from '../config';
import DeviceStatusBar from '../components/DeviceStatusBar';
import { useToast } from '../components/Toast';

export default function Dashboard() {
  const { addToast } = useToast();
  const navigate = useNavigate();
  const [dashboard, setDashboard] = useState(null);
  const [info, setInfo] = useState(null);
  const [activity, setActivity] = useState([]);
  const [llmStatus, setLlmStatus] = useState(null);
  const [llmPresets, setLlmPresets] = useState([]);
  const [channelStats, setChannelStats] = useState({ active_channels: [], details: {}, channel_count: 0 });
  const [genuiProviders, setGenuiProviders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [baselineSummary, setBaselineSummary] = useState(null);
  const [quickActionLoading, setQuickActionLoading] = useState(null);
  const [quickActionResult, setQuickActionResult] = useState(null);
  const [genesisStats, setGenesisStats] = useState(null);

  const refresh = useCallback(() => {
    Promise.all([
      fetch(`${API}/api/dashboard`).then(r => r.json()).catch(e => { addToast(e.message || 'Failed to load dashboard'); return null; }),
      fetch(`${API}/api/system/info`).then(r => r.json()).catch(e => { addToast(e.message || 'Failed to load system info'); return null; }),
      fetch(`${API}/api/activity`).then(r => r.json()).catch(e => { addToast(e.message || 'Failed to load activity'); return null; }),
      fetch(`${API}/api/llm/status`).then(r => r.json()).catch(e => { addToast(e.message || 'Failed to check LLM status'); return null; }),
      fetch(`${API}/api/llm/presets`).then(r => r.json()).catch(e => { addToast(e.message || 'Failed to load presets'); return null; }),
      fetch(`${API}/api/channels`).then(r => r.json()).catch(e => { addToast(e.message || 'Failed to load channels'); return null; }),
      fetch(`${API}/api/genui/providers`).then(r => r.json()).catch(e => { addToast(e.message || 'Failed to load providers'); return null; }),
      fetch(`${API}/api/baseline/summary`).then(r => r.json()).catch(() => null),
      fetch(`${API}/api/tool-genesis/stats`).then(r => r.json()).catch(() => null),
    ]).then(([dash, sys, act, llm, presets, channels, genui, baseline, genesis]) => {
      if (dash) setDashboard(dash);
      if (sys) setInfo(sys);
      if (act) setActivity(act.entries || []);
      if (llm && !llm.error) setLlmStatus(llm);
      if (presets && !presets.error) setLlmPresets(presets.presets || []);
      if (channels && !channels.error) setChannelStats(channels);
      if (genui && !genui.error) setGenuiProviders(genui.providers || []);
      if (baseline && !baseline.error && baseline.metrics_tracked > 0) setBaselineSummary(baseline);
      if (genesis && !genesis.error && (genesis.sequences_tracked > 0 || genesis.tools_generated > 0)) setGenesisStats(genesis);
      setLoading(false);
    });
  }, [addToast]);

  useEffect(() => {
    refresh();
    let ws;
    try {
      ws = new WebSocket(WS_URL);
      ws.onmessage = (evt) => {
        try {
          const msg = JSON.parse(evt.data);
          if (msg.type === 'state_push' && msg.event === 'dashboard_update' && msg.data) {
            setDashboard(msg.data);
          }
        } catch (e) { addToast(e.message || 'Failed to parse dashboard update'); }
      };
    } catch (e) { addToast(e.message || 'Dashboard connection error'); }
    return () => { if (ws) ws.close(); };
  }, [refresh]);

  useEffect(() => {
    const interval = setInterval(() => {
      fetch(`${API}/api/llm/status`).then(r => r.json()).then(setLlmStatus).catch(() => {});
    }, 15000);
    return () => clearInterval(interval);
  }, []);

  const executeQuickAction = async (action) => {
    setQuickActionLoading(action);
    setQuickActionResult(null);
    try {
      const res = await fetch(`${API}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hop: 'client', type: 'text_command', payload: { text: action } }),
      });
      const data = await res.json();
      const text = data.text || data.payload?.text || data.reply || data.message || JSON.stringify(data);
      setQuickActionResult({ action, result: text });
      setTimeout(() => setQuickActionResult(null), 15000);
    } catch (err) {
      setQuickActionResult({ action, result: `Error: ${err.message}` });
      setTimeout(() => setQuickActionResult(null), 8000);
    }
    setQuickActionLoading(null);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="w-6 h-6 border-2 border-feral-accent border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (!dashboard && !info) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-4">
        <WifiOff size={40} className="opacity-30" />
        <p className="text-sm opacity-50">Cannot connect to FERAL Brain</p>
        <button onClick={refresh} className="px-4 py-2 bg-feral-card border border-feral-border rounded-lg text-sm hover:bg-feral-card-hover">
          Retry
        </button>
      </div>
    );
  }

  const d = dashboard || {};
  const mem = d.memory || info?.memory || {};
  const health = d.health || {};
  const devices = d.devices || [];
  const deviceCount = d.device_count || 0;
  const sessionCount = d.session_count || 0;
  const skillsCount = d.skills_count || info?.skills?.length || 0;

  return (
    <div className="h-full overflow-y-auto">
      {devices.length > 0 && (
        <DeviceStatusBar devices={devices} hr={health.heart_rate} />
      )}
      <div className="max-w-6xl mx-auto p-4 lg:p-8 space-y-5">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Control Center</h1>
            <p className="text-xs text-feral-text-muted mt-0.5">
              FERAL Brain v{info?.version || '2026.6.26'}
              {d.llm_available && <span className="ml-2 text-emerald-400">LLM ready</span>}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={refresh} className="p-2 rounded-lg hover:bg-feral-card transition">
              <RefreshCw size={16} className="text-feral-text-secondary" />
            </button>
            <button
              onClick={() => navigate('/chat')}
              className="flex items-center gap-2 px-4 py-2 bg-feral-accent text-white rounded-lg text-sm font-medium hover:bg-feral-accent/90 transition active:scale-95"
            >
              <MessageSquare size={14} /> Chat
            </button>
          </div>
        </div>

        {/* Top Status Row */}
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <StatusCard icon={Wifi}     label="Sessions" value={sessionCount} status="ok" />
          <StatusCard icon={Cpu}      label="Devices"  value={deviceCount}  status={deviceCount > 0 ? 'ok' : 'muted'}
            subtitle={deviceCount > 0 ? `${deviceCount} connected` : 'none connected'} />
          <StatusCard icon={Puzzle}   label="Skills"   value={skillsCount}  status="accent" />
          <StatusCard icon={Activity} label="Audio"    value={d.audio_available ? 'Ready' : 'Off'}
            status={d.audio_available ? 'ok' : 'muted'} />
        </div>

        {/* Main Grid: Devices + Health */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
          <div className="bg-feral-card border border-feral-border rounded-xl p-5">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <Bluetooth size={16} className="text-feral-accent" />
                <h2 className="font-semibold text-sm">Connected Devices</h2>
              </div>
              <button onClick={() => navigate('/settings')}
                className="text-xs text-feral-accent hover:underline flex items-center gap-1">
                Manage <ChevronRight size={12} />
              </button>
            </div>
            {devices.length > 0 ? (
              <div className="space-y-2">
                {devices.map(dev => (
                  <div key={dev.node_id} className="flex items-center gap-3 bg-feral-bg/30 rounded-lg px-4 py-3">
                    <div className="w-2 h-2 rounded-full bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.5)]" />
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-mono truncate">{dev.node_id}</div>
                      <div className="text-xs text-feral-text-muted capitalize">{dev.type}</div>
                    </div>
                    <Radio size={14} className="text-emerald-400" />
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-center py-8">
                <Bluetooth size={28} className="mx-auto opacity-20 mb-3" />
                <p className="text-sm text-feral-text-muted">No devices connected</p>
                <p className="text-xs text-feral-text-muted mt-1">Use the hardware daemon or phone bridge to connect</p>
                <button onClick={() => navigate('/settings')} className="mt-3 text-xs text-feral-accent hover:underline">
                  Connect a device
                </button>
              </div>
            )}
          </div>

          <div className="bg-feral-card border border-feral-border rounded-xl p-5">
            <div className="flex items-center gap-2 mb-4">
              <Heart size={16} className="text-rose-400" />
              <h2 className="font-semibold text-sm">Live Health</h2>
            </div>
            {Object.keys(health).length > 0 ? (
              <div className="grid grid-cols-3 gap-4">
                {health.heart_rate && <HealthMetric icon={Heart}       label="Heart Rate" value={`${health.heart_rate}`} unit="bpm" status="critical" />}
                {health.spo2        && <HealthMetric icon={Wind}        label="SpO2"       value={`${health.spo2}`}       unit="%"   status="accent" />}
                {health.temperature && <HealthMetric icon={Thermometer} label="Temp"       value={`${health.temperature}`} unit="°C"  status="warning" />}
              </div>
            ) : (
              <div className="text-center py-8">
                <Heart size={28} className="mx-auto opacity-20 mb-3" />
                <p className="text-sm text-feral-text-muted">No health data</p>
                <p className="text-xs text-feral-text-muted mt-1">Connect a wristband or phone to stream biometrics</p>
              </div>
            )}
          </div>
        </div>

        {/* Baseline Summary */}
        {baselineSummary && (
          <div className="bg-feral-card border border-feral-border rounded-xl p-5">
            <div className="flex items-center gap-2 mb-4">
              <TrendingUp size={16} className="text-feral-accent" />
              <h2 className="font-semibold text-sm">Baseline Learning</h2>
              <span className="ml-auto text-[10px] text-feral-text-muted">
                {baselineSummary.metrics_tracked} metrics tracked
              </span>
            </div>
            {baselineSummary.categories?.length > 0 && (
              <div className="flex flex-wrap gap-2 mb-3">
                {baselineSummary.categories.map(cat => (
                  <span key={cat} className="text-[11px] bg-feral-accent/10 text-feral-accent px-2.5 py-1 rounded-full capitalize">
                    {cat}
                  </span>
                ))}
              </div>
            )}
            <div className="grid grid-cols-2 gap-3">
              <div className="bg-feral-bg/30 border border-feral-border rounded-lg p-3">
                <div className="text-xs text-feral-text-secondary uppercase tracking-wider mb-1">Metrics</div>
                <div className="text-xl font-bold text-feral-accent">{baselineSummary.metrics_tracked}</div>
              </div>
              <div className="bg-feral-bg/30 border border-feral-border rounded-lg p-3">
                <div className="text-xs text-feral-text-secondary uppercase tracking-wider mb-1">Recent Alerts</div>
                <div className={`text-xl font-bold ${baselineSummary.recent_alerts > 0 ? 'text-amber-400' : 'text-emerald-400'}`}>
                  {baselineSummary.recent_alerts}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Tool Genesis */}
        {genesisStats && (
          <div className="bg-feral-card border border-feral-border rounded-xl p-5">
            <div className="flex items-center gap-2 mb-4">
              <Sparkles size={16} className="text-violet-400" />
              <h2 className="font-semibold text-sm">Tool Genesis</h2>
              <span className="ml-auto text-[10px] text-feral-text-muted">auto-generated tools</span>
            </div>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              <div className="bg-feral-bg/30 border border-feral-border rounded-lg p-3">
                <div className="text-xs text-feral-text-secondary uppercase tracking-wider mb-1">Sequences</div>
                <div className="text-xl font-bold text-violet-400">{genesisStats.sequences_tracked}</div>
              </div>
              <div className="bg-feral-bg/30 border border-feral-border rounded-lg p-3">
                <div className="text-xs text-feral-text-secondary uppercase tracking-wider mb-1">Proposals</div>
                <div className="text-xl font-bold text-amber-400">{genesisStats.proposals_ready}</div>
              </div>
              <div className="bg-feral-bg/30 border border-feral-border rounded-lg p-3">
                <div className="text-xs text-feral-text-secondary uppercase tracking-wider mb-1">Generated</div>
                <div className="text-xl font-bold text-emerald-400">{genesisStats.tools_generated}</div>
              </div>
              <div className="bg-feral-bg/30 border border-feral-border rounded-lg p-3">
                <div className="text-xs text-feral-text-secondary uppercase tracking-wider mb-1">Total Uses</div>
                <div className="text-xl font-bold text-feral-accent">{genesisStats.total_uses}</div>
              </div>
            </div>
          </div>
        )}

        {/* Provider / Channel Plane */}
        <div className="bg-feral-card border border-feral-border rounded-xl p-5">
          <div className="flex items-center gap-2 mb-4">
            <Sparkles size={16} className="text-feral-accent" />
            <h2 className="font-semibold text-sm">Provider and Channel Plane</h2>
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <div className="bg-feral-bg/30 border border-feral-border rounded-lg p-3">
              <div className="text-xs text-feral-text-secondary uppercase tracking-wider mb-2">LLM Provider</div>
              <div className="text-sm font-semibold capitalize">{llmStatus?.provider || info?.config?.llm?.provider || 'unknown'}</div>
              <div className="text-xs text-feral-text-muted font-mono mt-1">{llmStatus?.model || info?.config?.llm?.model || 'n/a'}</div>
              <div className={`text-[11px] mt-2 ${llmStatus?.available ? 'text-emerald-400' : 'text-amber-400'}`}>
                {llmStatus?.available ? 'Connected' : 'Fallback / unavailable'}
              </div>
              <div className="text-[11px] text-feral-text-muted mt-1">Presets: {llmPresets.length}</div>
            </div>
            <div className="bg-feral-bg/30 border border-feral-border rounded-lg p-3">
              <div className="text-xs text-feral-text-secondary uppercase tracking-wider mb-2">Channels</div>
              <div className="text-sm font-semibold">{channelStats.channel_count || 0} active</div>
              {(channelStats.active_channels || []).length > 0 ? (
                <div className="mt-2 space-y-1">
                  {(channelStats.active_channels || []).map((ch) => (
                    <div key={ch} className="text-[11px] text-feral-text-secondary flex items-center justify-between">
                      <span className="capitalize">{ch}</span>
                      <span className="text-feral-text-muted">{channelStats.details?.[ch]?.known_chats || 0} chats</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-[11px] text-feral-text-muted mt-2">No active channels</div>
              )}
            </div>
            <div className="bg-feral-bg/30 border border-feral-border rounded-lg p-3">
              <div className="text-xs text-feral-text-secondary uppercase tracking-wider mb-2">Providers + Devices</div>
              <div className="text-[11px] text-feral-text-secondary">GenUI Providers: {genuiProviders.length}</div>
              <div className="text-[11px] text-feral-text-secondary mt-1">Connected Devices: {devices.length}</div>
              {genuiProviders.length > 0 && (
                <div className="mt-2 space-y-1">
                  {genuiProviders.slice(0, 3).map((p) => (
                    <div key={p.provider_id} className="text-[11px] text-feral-text-secondary truncate">
                      {p.name || p.provider_id} · {p.components?.length || 0} comps · {p.surface_ids?.length || 0} surfaces · {p.cache_policy?.mode || 'static'} cache
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Quick Actions */}
        <div className="bg-feral-card border border-feral-border rounded-xl p-5">
          <div className="flex items-center gap-2 mb-4">
            <Zap size={16} className="text-feral-accent" />
            <h2 className="font-semibold text-sm">Quick Actions</h2>
          </div>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <QuickAction icon={Search} label="Search the Web" onClick={() => executeQuickAction("What's the latest tech news?")} loading={quickActionLoading === "What's the latest tech news?"} />
            <QuickAction icon={Sun} label="Get Weather" onClick={() => executeQuickAction("What's the weather like right now?")} loading={quickActionLoading === "What's the weather like right now?"} />
            <QuickAction icon={Brain} label="Memory Status" onClick={() => executeQuickAction("Show me my memory stats")} loading={quickActionLoading === "Show me my memory stats"} />
            <QuickAction icon={Globe} label="System Check" onClick={() => executeQuickAction("Run a system health check")} loading={quickActionLoading === "Run a system health check"} />
          </div>
          {quickActionResult && (
            <div className="mt-3 bg-feral-bg/30 border border-feral-border rounded-lg p-4 animate-in fade-in duration-200">
              <div className="flex items-center justify-between mb-2">
                <span className="text-[11px] font-medium text-feral-accent">{quickActionResult.action}</span>
                <button
                  onClick={() => setQuickActionResult(null)}
                  className="text-feral-text-muted hover:text-feral-text text-xs transition"
                >
                  ×
                </button>
              </div>
              <div className="text-[13px] text-feral-text-secondary leading-relaxed whitespace-pre-wrap break-words">
                {quickActionResult.result}
              </div>
            </div>
          )}
        </div>

        {/* Memory + System Status */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
          <div className="bg-feral-card border border-feral-border rounded-xl p-5">
            <div className="flex items-center gap-2 mb-4">
              <Database size={16} className="text-feral-accent" />
              <h2 className="font-semibold text-sm">Memory</h2>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <MemoryMetric label="Notes" value={mem.notes || 0} icon={BookOpen} />
              <MemoryMetric label="Episodes" value={mem.episodes || 0} icon={Clock} />
              <MemoryMetric label="Knowledge" value={mem.knowledge_triples || 0} icon={Brain} />
              <MemoryMetric label="Sessions" value={mem.active_working_sessions || 0} icon={Zap} />
            </div>
          </div>

          <div className="bg-feral-card border border-feral-border rounded-xl p-5">
            <div className="flex items-center gap-2 mb-4">
              <Shield size={16} className="text-feral-accent" />
              <h2 className="font-semibold text-sm">System Status</h2>
            </div>
            <div className="space-y-2">
              <FeatureRow label="WASM Sandbox" active={d.wasm_available} />
              <FeatureRow label="Wake Word" active={d.wake_word_enabled} />
              <FeatureRow label="Federated Sync" active={d.sync?.running} />
              <FeatureRow label="Audio Pipeline" active={d.audio_available} />
              <FeatureRow label="LLM Connected" active={d.llm_available} />
              <FeatureRow label="Sync Peers" active={(d.sync?.peer_count || 0) > 0} detail={`${d.sync?.peer_count || 0} peers`} />
            </div>
          </div>
        </div>

        {/* Activity Feed */}
        <div className="bg-feral-card border border-feral-border rounded-xl p-5">
          <div className="flex items-center gap-2 mb-4">
            <Activity size={16} className="text-emerald-400" />
            <h2 className="font-semibold text-sm">Activity Feed</h2>
          </div>
          {activity.length > 0 ? (
            <div className="space-y-2 max-h-64 overflow-y-auto">
              {activity.slice().reverse().map((entry, i) => (
                <div key={i} className="flex items-center gap-3 text-xs bg-feral-bg/20 rounded-lg px-3 py-2">
                  <div className="w-1.5 h-1.5 rounded-full bg-emerald-400 flex-shrink-0" />
                  <span className="text-feral-text-secondary font-mono flex-shrink-0">{new Date(entry.timestamp * 1000).toLocaleTimeString()}</span>
                  <span className="text-feral-text-secondary capitalize font-medium">{entry.action}</span>
                  <span className="text-feral-text-muted truncate">{entry.detail}</span>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-xs text-feral-text-muted text-center py-4">No activity yet — start chatting or connect a device</p>
          )}
        </div>

        {/* Skills */}
        {info?.skills && info.skills.length > 0 && (
          <div className="bg-feral-card border border-feral-border rounded-xl p-5">
            <div className="flex items-center gap-2 mb-4">
              <Puzzle size={16} className="text-feral-accent" />
              <h2 className="font-semibold text-sm">Skills ({info.skills.length})</h2>
            </div>
            <div className="flex flex-wrap gap-2">
              {info.skills.map(s => (
                <span key={s.skill_id} className="text-xs bg-feral-bg/30 text-feral-text-secondary px-3 py-1.5 rounded-full">
                  {s.name}<span className="text-feral-text-muted ml-1">·{s.endpoints}</span>
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

const STATUS_COLORS = {
  ok:       'text-emerald-400',
  warning:  'text-amber-400',
  critical: 'text-rose-400',
  accent:   'text-feral-accent',
  muted:    'text-feral-text-muted',
};

function StatusCard({ icon: Icon, label, value, subtitle, status = 'accent' }) {
  const color = STATUS_COLORS[status] || STATUS_COLORS.accent;
  return (
    <div className="bg-feral-card border border-feral-border rounded-xl p-4">
      <div className="flex items-center gap-2 mb-2">
        <Icon size={14} className={color} />
        <span className="text-[10px] text-feral-text-secondary uppercase tracking-wider">{label}</span>
      </div>
      <div className={`text-2xl font-bold ${color}`}>{value}</div>
      {subtitle && <div className="text-[10px] text-feral-text-muted mt-0.5 truncate">{subtitle}</div>}
    </div>
  );
}

function MemoryMetric({ label, value, icon: Icon }) {
  return (
    <div className="flex items-center gap-3 bg-feral-bg/20 rounded-lg px-3 py-2.5">
      <Icon size={14} className="opacity-40 flex-shrink-0" />
      <div>
        <div className="text-lg font-bold leading-none">{value}</div>
        <div className="text-[10px] text-feral-text-secondary mt-0.5">{label}</div>
      </div>
    </div>
  );
}

function HealthMetric({ icon: Icon, label, value, unit, status = 'accent' }) {
  const color = STATUS_COLORS[status] || STATUS_COLORS.accent;
  return (
    <div className="text-center">
      <Icon size={18} className={`mx-auto mb-2 ${color}`} />
      <div className={`text-2xl font-bold ${color}`}>{value}</div>
      <div className="text-[10px] text-feral-text-secondary">{unit}</div>
      <div className="text-[10px] text-feral-text-muted mt-0.5">{label}</div>
    </div>
  );
}

function FeatureRow({ label, active, detail }) {
  return (
    <div className="flex items-center justify-between bg-feral-bg/20 rounded-lg px-3 py-2">
      <span className="text-xs text-feral-text-secondary">{label}</span>
      <div className="flex items-center gap-2">
        {detail && <span className="text-[10px] text-feral-text-muted">{detail}</span>}
        <div className={`w-2 h-2 rounded-full ${active ? 'bg-emerald-400' : 'bg-feral-text-muted/30'}`} />
      </div>
    </div>
  );
}

function QuickAction({ icon: Icon, label, onClick, loading }) {
  return (
    <button onClick={onClick} disabled={loading}
      className="flex items-center gap-2 bg-feral-bg/30 hover:bg-feral-bg/50 rounded-lg px-4 py-3 text-sm transition text-left disabled:opacity-50">
      {loading
        ? <Loader2 size={16} className="animate-spin text-feral-accent flex-shrink-0" />
        : <Icon size={16} className="text-feral-accent flex-shrink-0" />
      }
      <span className="text-xs text-feral-text-secondary">{label}</span>
    </button>
  );
}
