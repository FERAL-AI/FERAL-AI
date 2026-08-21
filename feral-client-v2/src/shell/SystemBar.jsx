import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Mic, MicOff, Sun, Moon } from 'lucide-react';
import { apiJson } from '../lib/api';
import { useTheme } from '../hooks/useTheme';
import { useVoice } from './VoiceContext';

/**
 * The system bar: real vitals, each one clickable.
 *
 * The approved design puts these across the top and says every one is a
 * control, not a readout: "Click the vitals in the bar." A number you
 * cannot act on is decoration, so each cell navigates to the surface
 * that explains it.
 *
 * What shipped before this was a search box in the same place. Search is
 * still there, on the palette, where it belongs.
 *
 * Every value is read from a live endpoint. Nothing here is derived from
 * configuration, because the whole point of a vitals bar is to report
 * what is true rather than what was intended: this codebase's dominant
 * defect class is a surface that claims success while doing nothing.
 */

const POLL_MS = 5000;

/** 12400 -> "12.4k". Tokens run large and the bar is narrow. */
export function compact(n) {
  const v = Number(n || 0);
  if (!Number.isFinite(v) || v <= 0) return '0';
  if (v < 1000) return String(Math.round(v));
  if (v < 1_000_000) return `${(v / 1000).toFixed(v < 10_000 ? 1 : 0)}k`;
  return `${(v / 1_000_000).toFixed(1)}M`;
}

/** Cost as the design renders it: $1.84, and never a bare 0. */
export function money(n) {
  const v = Number(n || 0);
  if (!Number.isFinite(v) || v <= 0) return '$0.00';
  return `$${v.toFixed(2)}`;
}

export default function SystemBar({ onOpenPalette }) {
  const navigate = useNavigate();
  const { theme, toggle: toggleTheme } = useTheme();
  const voice = useVoice();
  const [v, setV] = useState({
    shells: 0, needs: 0, tokens: 0, devices: 0, cost: 0, autonomy: '',
  });
  const timer = useRef(null);

  const load = useCallback(async () => {
    const [jobs, approvals, dash] = await Promise.allSettled([
      apiJson('/api/jobs?limit=60'),
      apiJson('/api/approvals'),
      apiJson('/api/dashboard'),
    ]);
    setV((prev) => {
      const next = { ...prev };
      if (jobs.status === 'fulfilled') {
        const items = Array.isArray(jobs.value?.items) ? jobs.value.items : [];
        next.shells = items.filter(
          (i) => i.kind === 'background_bash' && i.status === 'running',
        ).length;
      }
      if (approvals.status === 'fulfilled') {
        next.needs = Number(approvals.value?.count || 0);
      }
      if (dash.status === 'fulfilled') {
        const d = dash.value || {};
        next.devices = Number(d.online_count ?? d.device_count ?? 0);
        next.tokens = Number(d.memory?.tokens ?? d.tokens_used ?? 0);
        next.cost = Number(d.cost_today ?? d.spend_today ?? 0);
        next.autonomy = String(d.autonomy || d.health?.autonomy_mode || '');
      }
      return next;
    });
  }, []);

  useEffect(() => {
    load();
    timer.current = setInterval(load, POLL_MS);
    return () => clearInterval(timer.current);
  }, [load]);

  // Only render a vital that has something to say. A bar of zeroes is
  // noise, and the design's bar is sparse for that reason.
  const cells = [
    {
      key: 'shells', glyph: '>_', value: String(v.shells),
      label: `${v.shells} shell job${v.shells === 1 ? '' : 's'} running`, to: '/jobs',
    },
    {
      key: 'needs', glyph: '!', value: String(v.needs),
      tone: v.needs > 0 ? 'warn' : 'plain',
      label: `${v.needs} waiting on you`, to: '/approvals',
    },
    v.tokens > 0 && {
      key: 'tokens', glyph: '', value: compact(v.tokens),
      label: 'tokens in memory', to: '/memory',
    },
    {
      key: 'devices', glyph: '', value: String(v.devices),
      label: `${v.devices} device${v.devices === 1 ? '' : 's'} online`, to: '/devices',
    },
    v.cost > 0 && {
      key: 'cost', glyph: '', value: money(v.cost),
      label: 'spent today', to: '/health',
    },
  ].filter(Boolean);

  return (
    <header className="v2-sysbar" role="banner">
      <button
        type="button"
        className="v2-sysbar-brand"
        onClick={() => navigate('/console')}
        title="Console"
      >
        <span className="v2-sysbar-dot" aria-hidden="true" />
        FERAL
      </button>

      <div className="v2-sysbar-vitals">
        {cells.map((c) => (
          <button
            key={c.key}
            type="button"
            className="v2-sysbar-vital"
            data-tone={c.tone || 'plain'}
            onClick={() => navigate(c.to)}
            title={c.label}
            aria-label={c.label}
          >
            {c.glyph && <span className="v2-sysbar-glyph" aria-hidden="true">{c.glyph}</span>}
            {c.value}
          </button>
        ))}
        {v.autonomy && (
          <button
            type="button"
            className="v2-sysbar-autonomy"
            onClick={() => navigate('/oversight')}
            title="Autonomy mode. Click for the supervisor and the kill switch."
          >
            autonomy <strong>{v.autonomy}</strong>
          </button>
        )}
      </div>

      <button
        type="button"
        className="v2-sysbar-cmd"
        onClick={onOpenPalette}
        title="Search, run a command, or ask (⌘K)"
        aria-label="Open the command palette"
      >
        <span aria-hidden="true">⌘K</span>
      </button>

      {/* Theme and voice used to live in a second bar at the same
          top:0, which simply covered this one. The design has one bar,
          so they moved here rather than fighting over the same pixels. */}
      <button
        type="button"
        className="v2-sysbar-icon"
        onClick={toggleTheme}
        title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
      >
        {theme === 'dark' ? <Sun size={13} aria-hidden="true" /> : <Moon size={13} aria-hidden="true" />}
      </button>
      <button
        type="button"
        className="v2-sysbar-icon"
        data-on={voice.active ? 'yes' : 'no'}
        // A toggle has to say it is a toggle. The retired Menubar set
        // this and the replacement dropped it, which is a real loss for
        // anyone on a screen reader: the button announced as a plain
        // action with no on/off state.
        aria-pressed={voice.active}
        onClick={voice.toggle}
        title={voice.active ? 'End voice session' : 'Start voice session'}
        aria-label={voice.active ? 'End voice session' : 'Start voice session'}
      >
        {voice.active ? <Mic size={13} aria-hidden="true" /> : <MicOff size={13} aria-hidden="true" />}
      </button>
    </header>
  );
}
