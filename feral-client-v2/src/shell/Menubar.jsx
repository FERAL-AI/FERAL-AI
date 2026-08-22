import React from 'react';
import { Mic, MicOff, Sun, Moon, Search } from 'lucide-react';
import { useConnectionStatus } from '../hooks/useConnectionStatus';
import { useTheme } from '../hooks/useTheme';
import { useVoice } from './VoiceContext';
import { useCommandPalette } from './PaletteContext';
import StatusDot from '../ui/StatusDot';

/**
 * Top menubar. Left: FERAL mark + connection state. Centre: the command
 * palette trigger. Right: theme + voice toggles.
 *
 * The header of this file used to end "Command palette + identity slot
 * in later", and that promise sat here through every release while the
 * bar navigated precisely nothing: two toggles and a brand mark. The
 * palette exists now and this is its most discoverable trigger, because
 * a search field that looks like a search field is found by people who
 * do not know that Cmd-K is a thing. It is a button rather than an
 * input: the real field lives in the dialog, and two fields for one
 * query is a query that gets typed into the wrong one.
 */

/*
 * The connection indicator used to be a bare <span aria-hidden="true"> with
 * an inline background colour and no text. It is the app's single global
 * "is the brain reachable" signal and it was, by construction, invisible to
 * assistive tech and unreadable to anyone who cannot separate the hues.
 * Routing it through StatusDot gets it both an accessible name and the
 * shape channel every other status indicator now carries.
 */
const CONNECTION_STATE = {
  open: { tone: 'live', label: 'Brain connected' },
  connecting: { tone: 'warn', label: 'Brain connecting' },
  error: { tone: 'error', label: 'Brain connection failed' },
};

export default function Menubar() {
  const { state } = useConnectionStatus();
  const voice = useVoice();
  const { theme, toggle: toggleTheme } = useTheme();
  const { open: paletteOpen, openPalette } = useCommandPalette();

  const connection = CONNECTION_STATE[state] || {
    tone: 'off',
    label: `Brain ${state || 'disconnected'}`,
  };

  const voiceLabel = voice.active ? 'End voice session' : 'Start voice session';

  return (
    <header className="v2-menubar" role="banner">
      <div className="v2-menubar-left">
        <StatusDot
          tone={connection.tone}
          label={connection.label}
          className="v2-menubar-dot"
        />
        <span className="v2-menubar-brand">FERAL</span>
        <span className="v2-menubar-version">v2</span>
      </div>
      <button
        type="button"
        className="v2-menubar-search"
        onClick={openPalette}
        aria-haspopup="dialog"
        aria-expanded={paletteOpen}
        aria-label="Open the command palette"
        title="Search, run a command, or ask (⌘K)"
      >
        <Search size={13} aria-hidden="true" />
        <span className="v2-menubar-search-text">Search or run a command</span>
        <kbd className="v2-menubar-search-kbd">⌘K</kbd>
      </button>
      <div className="v2-menubar-right" aria-label="Menubar actions">
        <button
          type="button"
          className="v2-menubar-theme"
          onClick={toggleTheme}
          aria-label={theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode'}
          title={theme === 'light' ? 'Switch to dark' : 'Switch to light'}
        >
          {theme === 'light' ? <Sun size={14} /> : <Moon size={14} />}
        </button>
        <button
          type="button"
          className={`v2-menubar-voice${voice.active ? ' is-active' : ''}`}
          onClick={() => voice.toggle()}
          aria-pressed={voice.active}
          aria-label={voiceLabel}
          title={voiceLabel}
          disabled={state !== 'open' && !voice.active}
        >
          {voice.active ? <Mic size={15} /> : <MicOff size={15} />}
          <span className="v2-menubar-voice-label">
            {voice.active ? 'Listening' : 'Voice'}
          </span>
        </button>
      </div>
    </header>
  );
}
