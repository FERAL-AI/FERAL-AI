import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import './styles/tokens.css';
import './index.css';
// Markdown stack stylesheets — imported once at the app root so they
// land in the shared bundle and every chat surface that mounts
// MarkdownMessage gets code highlighting + math typesetting for free.
import 'highlight.js/styles/github-dark.css';
import 'katex/dist/katex.min.css';
import { bootstrapLocalApiKey, maybeRedirectToSetup } from './bootstrap';

// Default to light mode. Users can flip to dark via the menubar toggle;
// the choice persists to localStorage.feral_ui_theme and wins on reload.
(function applyTheme() {
  if (typeof document === 'undefined' || typeof localStorage === 'undefined') return;
  const pref = (() => { try { return localStorage.getItem('feral_ui_theme'); } catch { return null; } })();
  const root = document.documentElement;
  if (pref === 'dark') root.classList.add('v2-dark');
  else root.classList.add('v2-light');
})();

bootstrapLocalApiKey();
maybeRedirectToSetup();

// PWA service worker — register after first paint so the SW install
// never blocks the SPA boot. Skipped in:
//   - Dev (Vite injects HMR; SW caching interferes with hot reloads).
//   - The /pair landing. Pairing is a one-shot unauthenticated flow:
//     a fresh phone visits /pair?t=<token>, opens a WebSocket to
//     /v1/node, then either becomes a paired browser_node or
//     navigates away. SW caching brings no benefit, and the
//     activate-time clients.claim() in sw.js is known to trigger an
//     iOS Safari page reload on first SW takeover, which silently
//     kills the in-flight WebSocket. See A4 live phone-pair test.
const _swPath = typeof window !== 'undefined' ? (window.location.pathname || '') : '';
const _swPathIsPair = _swPath.startsWith('/pair') || _swPath.startsWith('/v2/pair');
if (
  typeof window !== 'undefined' &&
  'serviceWorker' in navigator &&
  window.location.protocol !== 'file:' &&
  !import.meta.env?.DEV &&
  !_swPathIsPair
) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch((err) => {
      // eslint-disable-next-line no-console
      console.warn('[FERAL] SW registration failed:', err);
    });
  });
}

class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, info) {
    // eslint-disable-next-line no-console
    console.error('FERAL v2 ErrorBoundary:', error, info);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="v2-error-shell">
          <div className="v2-error-card">
            <h1>Something broke</h1>
            <p>{this.state.error?.message}</p>
            <button type="button" onClick={() => window.location.reload()}>Reload</button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary>
      {/*
       * Basename detects the mount point at runtime so the same built
       * bundle works at /  (default — v2 is the only UI) and at /v2/
       * (alias retained for back-compat).
       */}
      <BrowserRouter basename={typeof window !== 'undefined' && window.location.pathname.startsWith('/v2') ? '/v2' : '/'}>
        <App />
      </BrowserRouter>
    </ErrorBoundary>
  </React.StrictMode>,
);
