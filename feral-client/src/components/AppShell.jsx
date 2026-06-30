import React, { useState, useEffect } from 'react';
import { Outlet, NavLink } from 'react-router-dom';
import { LayoutDashboard, MessageSquare, Settings, Cpu, ListChecks, Clock, Sun, Moon, BrainCircuit, Crosshair, Plug } from 'lucide-react';
import { useTheme } from '../hooks/useTheme';
import { API_BASE as API } from '../config';
import TheOrb from './TheOrb';

const navItems = [
  { to: '/', icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/chat', icon: MessageSquare, label: 'Chat' },
  { to: '/taskflows', icon: ListChecks, label: 'Flows' },
  { to: '/timeline', icon: Clock, label: 'Timeline' },
  { to: '/ambient', icon: Moon, label: 'Ambient', external: true },
  { to: '/glass-brain', icon: BrainCircuit, label: 'Glass Brain' },
  { to: '/intents', icon: Crosshair, label: 'Intents' },
  { to: '/settings', icon: Settings, label: 'Settings' },
];

export default function AppShell() {
  const { theme, toggle: toggleTheme } = useTheme();
  const [appVersion, setAppVersion] = useState('...');
  const [somatic, setSomatic] = useState(null);

  useEffect(() => {
    fetch(`${API}/health`).then(r => r.json()).then(d => setAppVersion(d.version || '2026.6.24')).catch(() => setAppVersion('2026.6.24'));
    const interval = setInterval(() => {
      fetch(`${API}/api/dashboard`).then(r => r.json()).then(d => {
        if (d.somatic) setSomatic(d.somatic);
      }).catch(() => {});
    }, 10000);
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="flex flex-col lg:flex-row h-screen bg-feral-bg text-feral-text">
      {/* Desktop Sidebar */}
      <nav className="hidden lg:flex w-[220px] flex-shrink-0 bg-feral-surface border-r border-feral-border flex-col">
        <div className="flex items-center gap-2 px-4 py-4 border-b border-feral-border">
          <img src="/feral-icon-48.png" alt="FERAL" width="32" height="32" style={{ borderRadius: 6 }} />
          <div>
            <span className="font-semibold text-sm tracking-wide block">FERAL</span>
            <span className="text-[10px] text-feral-text-muted">Unleashed AI</span>
          </div>
        </div>

        <div className="flex-1 py-3 px-2.5 space-y-0.5">
          {navItems.map(({ to, icon: Icon, label, external }) =>
            external ? (
              <a
                key={to}
                href={to}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-3 px-3 py-2.5 rounded-lg transition-all text-[13px] font-medium text-feral-text-secondary hover:text-feral-text hover:bg-feral-card-hover"
              >
                <Icon size={17} className="flex-shrink-0" />
                <span>{label}</span>
              </a>
            ) : (
              <NavLink
                key={to}
                to={to}
                end={to === '/'}
                className={({ isActive }) =>
                  `flex items-center gap-3 px-3 py-2.5 rounded-lg transition-all text-[13px] font-medium ${
                    isActive
                      ? 'bg-feral-accent-dim text-feral-accent'
                      : 'text-feral-text-secondary hover:text-feral-text hover:bg-feral-card-hover'
                  }`
                }
              >
                <Icon size={17} className="flex-shrink-0" />
                <span>{label}</span>
              </NavLink>
            )
          )}
        </div>

        {somatic && somatic.cognitive_load > 0 && (
          <div style={{ padding: '8px 16px', fontSize: 11, color: '#71717a', borderTop: '1px solid rgba(255,255,255,0.06)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <div style={{
                width: 8, height: 8, borderRadius: '50%',
                background: somatic.cognitive_load > 0.7 ? '#ef4444' : somatic.cognitive_load > 0.4 ? '#f59e0b' : '#10b981',
              }} />
              <span>Cognitive Load: {Math.round(somatic.cognitive_load * 100)}%</span>
            </div>
            {somatic.heart_rate > 0 && <div style={{ marginTop: 2 }}>HR: {Math.round(somatic.heart_rate)} bpm</div>}
          </div>
        )}

        <NavLink
          to="/settings"
          className="flex items-center gap-2 mx-3 mb-2 px-3 py-2 rounded-lg text-[12px] font-medium text-feral-accent bg-feral-accent-dim hover:bg-feral-accent-glow transition"
          title="Pair device — opens Settings > Devices"
        >
          <Plug size={14} className="flex-shrink-0" />
          <span>Pair device</span>
        </NavLink>

        <div className="px-4 py-4 border-t border-feral-border flex items-center justify-between">
          <div className="flex items-center gap-2 text-[11px] text-feral-text-muted">
            <Cpu size={11} />
            <span>v{appVersion}</span>
          </div>
          <button
            onClick={toggleTheme}
            className="p-1.5 rounded-lg text-feral-text-muted hover:text-feral-text hover:bg-feral-card-hover transition"
            title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          >
            {theme === 'dark' ? <Sun size={14} /> : <Moon size={14} />}
          </button>
        </div>
      </nav>

      <main className="flex-1 overflow-hidden">
        <Outlet />
      </main>

      {/* Mobile Bottom Tab Bar */}
      <nav className="lg:hidden flex-shrink-0 bg-feral-surface border-t border-feral-border flex items-center justify-around px-1 py-1.5 safe-area-bottom">
        {navItems.map(({ to, icon: Icon, label, external }) =>
          external ? (
            <a
              key={to}
              href={to}
              target="_blank"
              rel="noopener noreferrer"
              className="flex flex-col items-center gap-0.5 px-3 py-1.5 rounded-lg transition-all text-feral-text-muted"
            >
              <Icon size={19} />
              <span className="text-[10px] font-medium">{label}</span>
            </a>
          ) : (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `flex flex-col items-center gap-0.5 px-3 py-1.5 rounded-lg transition-all ${
                  isActive ? 'text-feral-accent' : 'text-feral-text-muted'
                }`
              }
            >
              <Icon size={19} />
              <span className="text-[10px] font-medium">{label}</span>
            </NavLink>
          )
        )}
      </nav>
    </div>
  );
}
