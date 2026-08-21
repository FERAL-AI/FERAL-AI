import React, { useCallback, useRef, useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import { Command } from 'lucide-react';
import { DOCK_ITEMS, isPaletteOnlyPath } from './navigation';
import { useCommandPalette } from './PaletteContext';
import { useMachineVitals } from '../hooks/useMachineVitals';
import DockStack, { HOLD_MS, isStackable } from './DockStack';

/**
 * Bottom dock — the eight pinned destinations plus the palette button.
 * One clean row, always.
 *
 * The eight tiles and the membership test for the palette button both
 * come out of `shell/navigation.js`. They used to be two hand-written
 * lists in two files: `PRIMARY_ITEMS` here and a `HUB_ROUTES` Set that
 * restated, by hand, which routes lived behind the popup. Adding a
 * destination to the popup and forgetting the Set left the whole Dock
 * unlit on arrival, which is a user staring at a navigation bar that
 * has stopped telling them where they are. `isPaletteOnlyPath` derives
 * that membership from the same list the palette renders, so the two
 * cannot drift apart.
 */
export default function Dock() {
  const location = useLocation();
  const { open, togglePalette } = useCommandPalette();
  const vitals = useMachineVitals();

  // The design's dock does more than magnify: "Jobs breathes with a fill
  // and a ring while something runs". A tile reports the state of what
  // it leads to, so you can see the machine working without opening
  // anything. Only two tiles have a live state to report.
  const tileState = (to) => {
    if (to === '/jobs' && vitals.running > 0) return 'busy';
    if (to === '/approvals' && vitals.needs > 0) return 'needs';
    return '';
  };
  // Press and hold, or right-click, fans a tile's contents out above the
  // dock. The hold has to cancel the click that would otherwise follow
  // it, or holding a tile both opens the stack and navigates away from
  // the page you wanted to act on.
  const [stack, setStack] = useState('');
  const holdRef = useRef(null);
  const heldRef = useRef(false);

  const startHold = useCallback((to) => {
    if (!isStackable(to)) return;
    heldRef.current = false;
    clearTimeout(holdRef.current);
    holdRef.current = setTimeout(() => {
      heldRef.current = true;
      setStack(to);
    }, HOLD_MS);
  }, []);

  const endHold = useCallback(() => { clearTimeout(holdRef.current); }, []);

  const onTileClick = useCallback((e) => {
    if (heldRef.current) {
      e.preventDefault();
      e.stopPropagation();
      heldRef.current = false;
    }
  }, []);

  const onTileContextMenu = useCallback((e, to) => {
    if (!isStackable(to)) return;
    // Also the keyboard context-menu key, which is the only way to reach
    // a stack without a pointer.
    e.preventDefault();
    setStack(to);
  }, []);

  const tileCount = (to) => {
    if (to === '/jobs') return vitals.running;
    if (to === '/approvals') return vitals.needs;
    return 0;
  };

  const paletteActive = isPaletteOnlyPath(location.pathname);

  return (
    <nav className="v2-dock" role="navigation" aria-label="Primary">
      {stack && <DockStack to={stack} onClose={() => setStack('')} />}
      <ul className="v2-dock-list">
        {DOCK_ITEMS.map(({ to, label, Icon }) => (
          <li key={to} className="v2-dock-item">
            <NavLink
              to={to}
              end={to === '/'}
              className={({ isActive }) =>
                `v2-dock-btn${isActive ? ' is-active' : ''}`
              }
              data-state={tileState(to)}
              onPointerDown={() => startHold(to)}
              onPointerUp={endHold}
              onPointerLeave={endHold}
              onClick={onTileClick}
              onContextMenu={(e) => onTileContextMenu(e, to)}
              aria-haspopup={isStackable(to) ? 'dialog' : undefined}
              title={tileCount(to) > 0 ? `${label} (${tileCount(to)})` : label}
            >
              <span className="v2-dock-ring" aria-hidden="true" />
              <span className="v2-dock-fill" aria-hidden="true" />
              <Icon size={20} aria-hidden="true" />
              <span className="v2-dock-label">{label}</span>
              {tileCount(to) > 0 && (
                <span className="v2-dock-count" aria-hidden="true">{tileCount(to)}</span>
              )}
            </NavLink>
          </li>
        ))}
        <li className="v2-dock-item v2-dock-item--divider">
          <button
            type="button"
            className={`v2-dock-btn${paletteActive || open ? ' is-active' : ''}`}
            onClick={togglePalette}
            aria-pressed={open}
            aria-haspopup="dialog"
            title="Command palette (⌘K)"
          >
            <Command size={20} aria-hidden="true" />
            <span className="v2-dock-label">Command</span>
          </button>
        </li>
      </ul>
    </nav>
  );
}
