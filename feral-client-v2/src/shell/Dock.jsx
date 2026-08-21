import React, { useEffect, useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import {
  MessageSquare, ListChecks, Cpu, LayoutDashboard, SquareStack,
  Settings as SettingsIcon, LayoutGrid, AppWindow, Shield,
} from 'lucide-react';
import HubLauncher from '../components/HubLauncher';

/**
 * Bottom dock: seven primary items + a Hub button that opens a popup with
 * the fifteen secondary destinations. One clean row, always.
 */
const PRIMARY_ITEMS = [
  { to: '/', label: 'Home', Icon: LayoutDashboard },
  { to: '/chat', label: 'Chat', Icon: MessageSquare },
  { to: '/flows', label: 'Flows', Icon: ListChecks },
  { to: '/devices', label: 'Devices', Icon: Cpu },
  { to: '/apps', label: 'Apps', Icon: AppWindow },
  { to: '/canvas', label: 'Canvas', Icon: SquareStack },
  // /oversight holds the supervisor audit trail and the kill switch.
  // It used to be reachable from exactly one place: a button in the
  // Glass Brain page header, itself a Hub entry. A safety control the
  // operator has to remember a path to is a safety control they will
  // not find while something is going wrong, so it is primary now.
  { to: '/oversight', label: 'Oversight', Icon: Shield },
];

// Routes that live behind the Hub popup. Membership drives the Hub
// button's active highlight, so anything the Hub can navigate to
// belongs here or the Dock goes blank on arrival. `/oversight` is
// deliberately absent: it has its own primary NavLink above, which
// highlights itself. `/memory/context` needs its own entry because
// the check is exact-pathname, not prefix.
const HUB_ROUTES = new Set([
  '/approvals',
  '/forge', '/skills', '/memory', '/memory/context', '/wiki', '/agents',
  '/identity', '/health', '/intents', '/timeline', '/glass-brain',
  '/marketplace', '/webhooks', '/geofences',
]);

export default function Dock() {
  const location = useLocation();
  const [hubOpen, setHubOpen] = useState(false);

  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setHubOpen((prev) => !prev);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const hubActive = HUB_ROUTES.has(location.pathname);

  return (
    <>
      <nav className="v2-dock" role="navigation" aria-label="Primary">
        <ul className="v2-dock-list">
          {PRIMARY_ITEMS.map(({ to, label, Icon }) => (
            <li key={to} className="v2-dock-item">
              <NavLink
                to={to}
                end={to === '/'}
                className={({ isActive }) =>
                  `v2-dock-btn${isActive ? ' is-active' : ''}`
                }
                title={label}
              >
                <Icon size={20} aria-hidden="true" />
                <span className="v2-dock-label">{label}</span>
              </NavLink>
            </li>
          ))}
          <li className="v2-dock-item v2-dock-item--divider">
            <button
              type="button"
              className={`v2-dock-btn${hubActive || hubOpen ? ' is-active' : ''}`}
              onClick={() => setHubOpen((prev) => !prev)}
              aria-pressed={hubOpen}
              title="Hub (⌘K)"
            >
              <LayoutGrid size={20} aria-hidden="true" />
              <span className="v2-dock-label">Hub</span>
            </button>
          </li>
          <li className="v2-dock-item">
            <NavLink
              to="/settings"
              className={({ isActive }) =>
                `v2-dock-btn${isActive ? ' is-active' : ''}`
              }
              title="Settings"
            >
              <SettingsIcon size={20} aria-hidden="true" />
              <span className="v2-dock-label">Settings</span>
            </NavLink>
          </li>
        </ul>
      </nav>
      <HubLauncher open={hubOpen} onClose={() => setHubOpen(false)} />
    </>
  );
}
