import React, { useCallback, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Mic, MicOff, Sun, Moon, PanelLeft, Terminal, ShieldCheck,
  Database, Smartphone, CircleDollarSign,
} from 'lucide-react';
import { useTheme } from '../hooks/useTheme';
import { useMachineVitals } from '../hooks/useMachineVitals';
import { useVoice } from './VoiceContext';
import VitalPopover, { compact, money } from './VitalPopover';

/**
 * The system bar: menu-bar extras, one per vital, each opening a popover.
 *
 * The approved design's bar is an instrument panel. Every extra is a
 * control: "Click the vitals in the bar." Its own note says each one
 * opens a popover whose rows you act from, which is the same argument
 * the rail and the dock stacks make.
 *
 * Two things were wrong with what shipped.
 *
 * The bar navigated instead of opening anything, so a number was a link
 * and never a control. And four of the seven vitals were wired to fields
 * that DO NOT EXIST on `/api/dashboard`: `cost_today`, `spend_today`,
 * `tokens_used` and `autonomy` are all absent from that payload, so cost
 * and autonomy read 0 and empty forever and were hidden by the
 * render-if-non-zero rule. The bar looked sparse because it was reading
 * nothing, not because the machine was idle.
 *
 * The memory vital was a misreading too. The design's `12.4k` is
 * labelled "episodes" in its own popover; it was implemented as a token
 * count, and `memory.tokens` does not exist either.
 *
 * Those numbers now come from real sources: the budget from the LLM
 * provider's own snapshot and the tier from the live ToolRunner, both
 * added to the dashboard payload, and episodes from `memory.episodes`.
 */

/** A vital renders only when the brain can actually answer it. */
export function visibleVitals(v) {
  return [
    // The brain itself. The sparkline animates only while something is
    // running, so an idle machine has a still bar rather than a fake
    // heartbeat.
    { k: 'brain', title: 'Brain', spark: true, live: v.running > 0 },
    {
      k: 'jobs', title: 'Running', Icon: Terminal, label: String(v.running || 0),
      dot: v.running > 0 ? 'run' : '',
      aria: `${v.running} running`,
    },
    {
      k: 'needs', title: 'Needs you', Icon: ShieldCheck,
      count: v.needs > 0 ? String(v.needs) : '',
      aria: `${v.needs} waiting on you`,
    },
    v.episodes > 0 && {
      k: 'mem', title: 'Memory', Icon: Database, label: compact(v.episodes),
      aria: `${v.episodes} episodes remembered`,
    },
    {
      k: 'dev', title: 'Devices', Icon: Smartphone, label: String(v.devices || 0),
      dot: v.devices > 0 ? 'ok' : '',
      aria: `${v.devices} devices online`,
    },
    // Shown whenever the brain reported a budget at all, including
    // $0.00. A cost of zero is information; a missing reading is not,
    // and the two used to look identical.
    v.costKnown && {
      k: 'cost', title: 'Today', Icon: CircleDollarSign, label: money(v.cost),
      aria: `${money(v.cost)} spent today`,
    },
    v.autonomy && {
      k: 'autonomy', title: 'Autonomy', label: v.autonomy, word: 'autonomy',
      aria: `Autonomy is ${v.autonomy}`,
    },
  ].filter(Boolean);
}

export default function SystemBar({ onOpenPalette, railOpen, onToggleRail }) {
  const navigate = useNavigate();
  const { theme, toggle: toggleTheme } = useTheme();
  const voice = useVoice();
  const v = useMachineVitals();
  const [open, setOpen] = useState('');

  const toggle = useCallback((k) => setOpen((cur) => (cur === k ? '' : k)), []);
  const vitals = visibleVitals(v);
  const current = vitals.find((x) => x.k === open);

  return (
    <header className="v2-sysbar" role="banner">
      <button
        type="button"
        className="v2-sysbar-icon"
        onClick={onToggleRail}
        aria-pressed={!railOpen}
        title={railOpen ? 'Collapse the rail (B)' : 'Show the rail (B)'}
        aria-label={railOpen ? 'Collapse the rail' : 'Show the rail'}
      >
        <PanelLeft size={13} aria-hidden="true" />
      </button>

      <button
        type="button"
        className="v2-sysbar-brand"
        onClick={() => navigate('/console')}
        title="Console"
      >
        <span className="v2-sysbar-mark" aria-hidden="true" />
        FERAL
      </button>

      <div className="v2-sysbar-space" />

      <div className="v2-sysbar-vitals">
        {vitals.map(({ k, title, Icon, label, count, dot, spark, live, word, aria }) => (
          <button
            key={k}
            type="button"
            className="v2-ext"
            aria-expanded={open === k}
            aria-haspopup="dialog"
            onClick={() => toggle(k)}
            title={aria || title}
            aria-label={aria || title}
          >
            {spark && (
              <span className={`v2-spark${live ? ' is-live' : ''}`} aria-hidden="true">
                <i /><i /><i /><i /><i />
              </span>
            )}
            {dot && <span className={`v2-ext-dot v2-ext-dot--${dot}`} aria-hidden="true" />}
            {Icon && <Icon size={13} aria-hidden="true" />}
            {word && <span className="v2-ext-word">{word}</span>}
            {label && <span className="v2-ext-label">{label}</span>}
            {count && <span className="v2-ext-count">{count}</span>}
          </button>
        ))}
      </div>

      <button
        type="button"
        className="v2-sysbar-cmd"
        onClick={onOpenPalette}
        aria-haspopup="dialog"
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
        // anyone on a screen reader.
        aria-pressed={voice.active}
        onClick={voice.toggle}
        title={voice.active ? 'End voice session' : 'Start voice session'}
        aria-label={voice.active ? 'End voice session' : 'Start voice session'}
      >
        {voice.active ? <Mic size={13} aria-hidden="true" /> : <MicOff size={13} aria-hidden="true" />}
      </button>

      {current && (
        <VitalPopover
          kind={current.k}
          title={current.title}
          vitals={v}
          onClose={() => setOpen('')}
        />
      )}
    </header>
  );
}
