import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Search, X, Plug, CornerDownLeft, Sparkles, Waves,
  Plus, Mic, MicOff, Sun, Moon,
} from 'lucide-react';
import useFocusTrap from '../ui/useFocusTrap';
import { useResource } from '../hooks/useResource';
import { deviceCounts } from '../components/DeviceTopology';
import { useTheme } from '../hooks/useTheme';
import { useVoice } from './VoiceContext';
import { useChatThread } from './ChatThreadContext';
import { GO_ITEMS, matchesQuery } from './navigation';

/**
 * CommandPalette — the second of the two navigation mechanisms, and the
 * only search surface in the shell.
 *
 * It replaces HubLauncher, which was a grid of fifteen tiles that
 * excluded seven of the eight Dock primaries: opening it and typing
 * "chat", "devices", "home", "flows", "apps", "canvas" or "settings"
 * matched nothing at all, because those destinations were only ever
 * Dock tiles. A search box that cannot find the seven things you use
 * most is not a search box.
 *
 * Three sections, in the order a person reaches for them:
 *
 *   Do   verbs. Things that happen here, without navigating.
 *   Go   entities. Every route in the shell, plus all sixteen Settings
 *        sections as `?section=` deep links, which Settings.jsx already
 *        honours.
 *   Ask  hands the typed query to the chat composer. This is the row
 *        that makes it FERAL's palette rather than a copy of anyone
 *        else's: the thing you typed is usually a question, and the
 *        brain is the one surface that can answer an arbitrary one.
 *
 * Arrow keys move the selection, Enter runs it, Escape closes.
 */

/**
 * The device-count CTA, carried over from HubLauncher unchanged in
 * behaviour.
 *
 * Both counts stay null on a failed fetch. They used to be reset to 0,
 * which trips the `paired === 0` branch and renders "No devices paired
 * yet" at an operator who may have five devices paired and a brain that
 * is merely unreachable. `deviceCounts` is the one shared derivation
 * (components/DeviceTopology.jsx) so Home, GlassBrain and this surface
 * cannot report three different numbers.
 */
function DeviceCta({ open, onGo }) {
  const { data: counts } = useResource('/api/dashboard', {
    enabled: open,
    silent: true,
    select: (d) => deviceCounts(d),
  });
  const pairedCount = counts ? counts.total : null;
  const onlineCount = counts ? counts.online : null;

  if (pairedCount === 0) {
    return (
      <button type="button" className="v2-cmdk-cta" onClick={() => onGo('/devices')}>
        <Plug size={14} aria-hidden="true" />
        <div>
          <div className="v2-cmdk-cta-title">Pair a device</div>
          <div className="v2-cmdk-cta-hint">No devices paired yet.</div>
        </div>
      </button>
    );
  }
  if (pairedCount > 0 && onlineCount === 0) {
    return (
      <button type="button" className="v2-cmdk-cta" onClick={() => onGo('/devices')}>
        <Plug size={14} aria-hidden="true" />
        <div>
          <div className="v2-cmdk-cta-title">
            {pairedCount === 1 ? '1 device paired, currently offline' : `${pairedCount} devices paired, none online`}
          </div>
          <div className="v2-cmdk-cta-hint">Re-open the device&apos;s FERAL app to bring it back online.</div>
        </div>
      </button>
    );
  }
  return null;
}

export default function CommandPalette({ open, onClose }) {
  const navigate = useNavigate();
  const inputRef = useRef(null);
  const dialogRef = useRef(null);
  const listRef = useRef(null);
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState(0);

  const voice = useVoice();
  const { theme, toggle: toggleTheme } = useTheme();
  const thread = useChatThread();

  const close = useCallback(() => { onClose?.(); }, [onClose]);

  const go = useCallback((to) => {
    navigate(to);
    close();
  }, [navigate, close]);

  /**
   * Verbs. Every one of these is an action that already exists in the
   * shell and was previously reachable from exactly one control each:
   * the mic and theme buttons in the Menubar, the "+" in the Chat
   * threads pane, and Cmd-Period for the ambient strip, which was an
   * undocumented chord bound to a layer most users never saw.
   */
  const doItems = useMemo(() => [
    {
      id: 'do:new-conversation',
      label: 'New conversation',
      desc: 'Start a fresh chat thread',
      Icon: Plus,
      run: async () => {
        if (thread?.startNewConversation) await thread.startNewConversation();
        navigate('/chat');
      },
    },
    {
      id: 'do:voice',
      label: voice.active ? 'End voice session' : 'Start voice session',
      desc: voice.active ? 'Stop listening' : 'Hold a conversation by voice',
      Icon: voice.active ? MicOff : Mic,
      run: () => voice.toggle(),
    },
    {
      id: 'do:theme',
      label: theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode',
      desc: 'Flip the interface theme',
      Icon: theme === 'light' ? Moon : Sun,
      run: () => toggleTheme(),
    },
    {
      id: 'do:ambient',
      label: 'Reveal the ambient layer',
      desc: 'Expand the background field and live-ops strip',
      Icon: Waves,
      run: () => {
        window.dispatchEvent(new CustomEvent('v2:ambient-expand'));
      },
    },
  ], [voice, theme, toggleTheme, thread, navigate]);

  const trimmed = query.trim();

  /**
   * One flat, ordered list of everything currently selectable. The
   * rendered sections read their slice out of this array, so the
   * keyboard cursor and what is on screen cannot disagree.
   */
  const rows = useMemo(() => {
    const matchedDo = doItems.filter((it) => matchesQuery(it, query));
    const matchedGo = GO_ITEMS.filter((it) => matchesQuery(it, query));
    const ask = trimmed
      ? [{
        id: 'ask',
        label: `Ask FERAL: ${trimmed}`,
        desc: 'Hand this to the chat composer',
        Icon: Sparkles,
        run: () => {
          thread?.setAskDraft?.(trimmed);
          navigate('/chat');
        },
      }]
      : [];
    return [
      ...matchedDo.map((it) => ({ ...it, section: 'Do' })),
      ...matchedGo.map((it) => ({ ...it, id: `go:${it.to}`, section: 'Go', run: () => navigate(it.to) })),
      ...ask.map((it) => ({ ...it, section: 'Ask' })),
    ];
  }, [doItems, query, trimmed, navigate, thread]);

  useEffect(() => { setCursor(0); }, [query]);

  useEffect(() => {
    if (!open) { setQuery(''); setCursor(0); return; }
    // Focus the search field once the popup is visible.
    const t = setTimeout(() => inputRef.current?.focus(), 30);
    return () => clearTimeout(t);
  }, [open]);

  // aria-modal="true" is a promise the rest of the page is inert. Same
  // hook Modal uses. `focusOnOpen: 'none'` because the effect above
  // deliberately puts initial focus in the search field, and no scroll
  // lock because this popup does not cover the whole viewport.
  useFocusTrap(open, () => dialogRef.current, {
    lockScroll: false,
    focusOnOpen: 'none',
  });

  const runRow = useCallback((row) => {
    if (!row) return;
    const result = row.run?.();
    // Close immediately for synchronous verbs; an async verb (creating
    // a conversation) still closes now, because leaving the palette up
    // over a navigation reads as "the click did nothing".
    if (result && typeof result.catch === 'function') result.catch(() => {});
    close();
  }, [close]);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); close(); return; }
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setCursor((c) => (rows.length ? (c + 1) % rows.length : 0));
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setCursor((c) => (rows.length ? (c - 1 + rows.length) % rows.length : 0));
        return;
      }
      if (e.key === 'Enter') {
        // Only when focus is in the palette. Enter inside a page form
        // that happens to be behind an open palette is not ours.
        if (!dialogRef.current?.contains(document.activeElement)) return;
        e.preventDefault();
        runRow(rows[cursor]);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, close, rows, cursor, runRow]);

  // Keep the highlighted row inside the scroll box. `block: 'nearest'`
  // so paging with the arrow keys does not yank the list around.
  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector('.v2-cmdk-row.is-cursor');
    el?.scrollIntoView?.({ block: 'nearest' });
  }, [cursor, open, rows.length]);

  if (!open) return null;

  let rendered = -1;
  const sections = ['Do', 'Go', 'Ask'];

  return (
    <div
      className="v2-cmdk-backdrop"
      role="presentation"
      onClick={(e) => { if (e.target === e.currentTarget) close(); }}
    >
      <div
        ref={dialogRef}
        className="v2-cmdk"
        role="dialog"
        aria-label="Command palette"
        aria-modal="true"
        tabIndex={-1}
      >
        <header className="v2-cmdk-head">
          <Search size={14} aria-hidden="true" />
          <input
            ref={inputRef}
            className="v2-cmdk-search"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search pages, run a command, or ask…"
            aria-label="Search commands and pages"
            aria-controls="v2-cmdk-list"
            autoComplete="off"
          />
          <button
            type="button"
            className="v2-btn v2-btn--ghost"
            onClick={close}
            aria-label="Close"
          >
            <X size={13} />
          </button>
        </header>

        {!trimmed && <DeviceCta open={open} onGo={go} />}

        <div className="v2-cmdk-list" id="v2-cmdk-list" ref={listRef} role="listbox" aria-label="Results">
          {sections.map((section) => {
            const inSection = rows.filter((r) => r.section === section);
            if (!inSection.length) return null;
            return (
              <div className="v2-cmdk-group" key={section}>
                <div className="v2-cmdk-group-label">{section}</div>
                {inSection.map((row) => {
                  rendered += 1;
                  const index = rendered;
                  const { Icon } = row;
                  return (
                    <button
                      key={row.id}
                      type="button"
                      role="option"
                      aria-selected={index === cursor}
                      className={`v2-cmdk-row${index === cursor ? ' is-cursor' : ''}`}
                      onMouseEnter={() => setCursor(index)}
                      onClick={() => runRow(row)}
                    >
                      <span className="v2-cmdk-row-icon" aria-hidden="true">
                        {Icon ? <Icon size={16} /> : null}
                      </span>
                      <span className="v2-cmdk-row-text">
                        <span className="v2-cmdk-row-label">{row.label}</span>
                        <span className="v2-cmdk-row-desc">{row.desc}</span>
                      </span>
                      {index === cursor && (
                        <span className="v2-cmdk-row-enter" aria-hidden="true">
                          <CornerDownLeft size={13} />
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            );
          })}
          {rows.length === 0 && (
            <div className="v2-cmdk-empty">No matches.</div>
          )}
        </div>

        <footer className="v2-cmdk-foot">
          <span><kbd>↑</kbd><kbd>↓</kbd> move</span>
          <span><kbd>↵</kbd> run</span>
          <span><kbd>esc</kbd> close</span>
        </footer>
      </div>
    </div>
  );
}
