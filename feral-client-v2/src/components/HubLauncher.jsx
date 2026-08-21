import React, { useEffect, useMemo, useRef, useState } from 'react';
import useFocusTrap from '../ui/useFocusTrap';
import { useNavigate } from 'react-router-dom';
import {
  Hammer, Wrench, Database, BookOpen, Users, UserCircle2,
  HeartPulse, Crosshair, Clock, BrainCircuit, Globe, MapPin, Store,
  Search, X, Plug, Shield, BrainCog, ShieldAlert } from 'lucide-react';
import { useResource } from '../hooks/useResource';
import { deviceCounts } from './DeviceTopology';

/**
 * HubLauncher: translucent stack popup anchored above the Dock. Holds the
 * 15 secondary navigation destinations that used to clutter the Dock.
 *
 * Triggered by the Hub button on the Dock or the ⌘K shortcut. Search
 * filters by name / description. Click an entry → navigate + close.
 */

const HUB_ITEMS = [
  { to: '/approvals', label: 'Needs you', Icon: ShieldAlert, desc: 'Tool calls blocked on your decision' },
  { to: '/forge', label: 'Forge', Icon: Hammer, desc: 'Tool Genesis drafts + promote' },
  { to: '/skills', label: 'Skills', Icon: Wrench, desc: 'Loaded skills + hot-reload' },
  { to: '/memory', label: 'Memory', Icon: Database, desc: 'Notes, episodes, execution log' },
  { to: '/wiki', label: 'Wiki', Icon: BookOpen, desc: 'Long-form knowledge + ingest' },
  { to: '/agents', label: 'Agents', Icon: Users, desc: 'Agent Mitosis specialists' },
  { to: '/identity', label: 'Identity', Icon: UserCircle2, desc: 'IDENTITY / SOUL / MEMORY editors' },
  { to: '/health', label: 'Health', Icon: HeartPulse, desc: 'Baseline metrics + alerts' },
  { to: '/intents', label: 'Intents', Icon: Crosshair, desc: 'Goal plans + today' },
  { to: '/timeline', label: 'Timeline', Icon: Clock, desc: 'Chronological activity' },
  { to: '/glass-brain', label: 'Brain', Icon: BrainCircuit, desc: 'Live 3D Glass Brain' },
  { to: '/marketplace', label: 'Market', Icon: Store, desc: 'Browse + install registry items' },
  { to: '/webhooks', label: 'Webhooks', Icon: Globe, desc: 'Inbound integrations' },
  { to: '/geofences', label: 'Places', Icon: MapPin, desc: 'Geofences + location' },
  // Both of these were reachable ONLY from two buttons inside the
  // Glass Brain page header. /oversight is the kill switch; a safety
  // control three clicks deep inside an unrelated page is not
  // reachable when it matters. It is now also a primary Dock item
  // (shell/Dock.jsx); it is listed here as well so ⌘K search finds it.
  { to: '/oversight', label: 'Oversight', Icon: Shield, desc: 'Supervisor audit + kill switch' },
  { to: '/memory/context', label: 'Memory context', Icon: BrainCog, desc: 'What multi-memory surfaced per LLM turn' },
];

export default function HubLauncher({ open, onClose }) {
  const navigate = useNavigate();
  const inputRef = useRef(null);
  const dialogRef = useRef(null);
  const [query, setQuery] = useState('');
  // Phase-1 truthfulness: show the Pair CTA when the operator has
  // never paired anything (`paired_count == 0`), not when the brain
  // happens to have zero LIVE daemons right now (`device_count == 0`).
  // The legacy `device_count` field counts only currently-online HUP
  // nodes, so a paired phone that backgrounded itself made the CTA
  // re-appear and lied to the user that they had nothing paired.
  // See audit-r6/08-status-truthfulness-audit.md row 21.
  //
  // The failure path reintroduced exactly the lie the comment above
  // describes. It was `.catch(() => { setOnlineCount(0);
  // setPairedCount(0); })`, which trips the `pairedCount === 0` branch
  // below and renders "Pair a device / No devices paired yet" at a
  // user who may have five devices paired and a brain that is merely
  // unreachable. Both counts stay null on failure, so neither CTA
  // renders and the Hub simply says nothing about devices.
  //
  // The third fallback chain that used to live here is gone. Home,
  // GlassBrain and this popup now share one derivation (`deviceCounts`
  // in components/DeviceTopology.jsx) so the same payload can no
  // longer produce three different device numbers on three surfaces.
  const { data: counts } = useResource('/api/dashboard', {
    enabled: open,
    silent: true,
    select: (d) => deviceCounts(d),
  });
  const pairedCount = counts ? counts.total : null;
  const onlineCount = counts ? counts.online : null;

  useEffect(() => {
    if (!open) { setQuery(''); return; }
    // Focus the search field once the popup is visible.
    setTimeout(() => inputRef.current?.focus(), 30);
  }, [open]);

  // This dialog declared aria-modal="true" while doing nothing to contain
  // focus, so Tab walked straight out into the page behind it. Same hook
  // Modal uses; `focusOnOpen: 'none'` because the effect above deliberately
  // puts initial focus in the search field, and no scroll lock because this
  // popup does not cover the whole viewport.
  useFocusTrap(open, () => dialogRef.current, {
    lockScroll: false,
    focusOnOpen: 'none',
  });

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') onClose?.();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const items = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return HUB_ITEMS;
    return HUB_ITEMS.filter((it) =>
      it.label.toLowerCase().includes(q) ||
      it.to.toLowerCase().includes(q) ||
      (it.desc || '').toLowerCase().includes(q),
    );
  }, [query]);

  if (!open) return null;

  const go = (to) => {
    navigate(to);
    onClose?.();
  };

  return (
    <div
      className="v2-hub-backdrop"
      role="presentation"
      onClick={(e) => { if (e.target === e.currentTarget) onClose?.(); }}
    >
      <div ref={dialogRef} className="v2-hub" role="dialog" aria-label="Hub launcher" aria-modal="true" tabIndex={-1}>
        <header className="v2-hub-head">
          <Search size={14} aria-hidden="true" />
          <input
            ref={inputRef}
            className="v2-hub-search"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search pages… (⌘K)"
            aria-label="Search hub items"
          />
          <button
            type="button"
            className="v2-btn v2-btn--ghost"
            onClick={() => onClose?.()}
            aria-label="Close"
          >
            <X size={13} />
          </button>
        </header>

        {pairedCount === 0 && !query && (
          <button
            type="button"
            className="v2-hub-cta"
            onClick={() => go('/devices')}
          >
            <Plug size={14} aria-hidden="true" />
            <div>
              <div className="v2-hub-cta-title">Pair a device</div>
              <div className="v2-hub-cta-hint">No devices paired yet.</div>
            </div>
          </button>
        )}
        {pairedCount > 0 && onlineCount === 0 && !query && (
          <button
            type="button"
            className="v2-hub-cta"
            onClick={() => go('/devices')}
          >
            <Plug size={14} aria-hidden="true" />
            <div>
              <div className="v2-hub-cta-title">
                {pairedCount === 1 ? '1 device paired, currently offline' : `${pairedCount} devices paired, none online`}
              </div>
              <div className="v2-hub-cta-hint">Re-open the device's FERAL app to bring it back online.</div>
            </div>
          </button>
        )}

        <div className="v2-hub-grid">
          {items.map(({ to, label, Icon, desc }) => (
            <button
              key={to}
              type="button"
              className="v2-hub-item"
              onClick={() => go(to)}
            >
              <div className="v2-hub-icon" aria-hidden="true">
                <Icon size={22} />
              </div>
              <div className="v2-hub-label">{label}</div>
              <div className="v2-hub-desc">{desc}</div>
            </button>
          ))}
          {items.length === 0 && (
            <div className="v2-p v2-p--muted">No matches.</div>
          )}
        </div>
      </div>
    </div>
  );
}
