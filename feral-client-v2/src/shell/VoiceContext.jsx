import React, { createContext, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useVoiceMode } from '../hooks/useVoiceMode';

const VoiceContext = createContext(null);

export function VoiceProvider({ children }) {
  const mode = useVoiceMode();

  /*
   * Whether a composer voice lane is mounted right now.
   *
   * The overlay needs this so it does not offer a second "End voice"
   * next to the lane's own, which put three ways to stop a session on
   * screen at once: the overlay's, the lane's, and the system bar's
   * global toggle.
   *
   * It is deliberately NOT derived from the route. The first version
   * read `useLocation().pathname.startsWith('/chat')`, which is a
   * different fact that happens to correlate today, and it made the
   * overlay unrenderable outside a Router: eight standalone tests broke
   * instantly. The question is "is there a lane", and the lane is the
   * only thing that actually knows.
   *
   * Counted rather than a boolean so two mounts, or a remount before an
   * unmount during a route transition, cannot leave it stuck on.
   */
  const [laneCount, setLaneCount] = useState(0);

  const value = useMemo(() => ({
    ...mode,
    laneMounted: laneCount > 0,
    registerVoiceLane: () => {
      setLaneCount((n) => n + 1);
      return () => setLaneCount((n) => Math.max(0, n - 1));
    },
  }), [mode, laneCount]);

  return (
    <VoiceContext.Provider value={value}>{children}</VoiceContext.Provider>
  );
}

/**
 * Declare that a composer voice lane is on screen.
 *
 * Call from the component that renders the lane. Registration is
 * refcounted and released on unmount.
 */
export function useRegisterVoiceLane(active) {
  const ctx = useContext(VoiceContext);
  const register = ctx?.registerVoiceLane;
  const releaseRef = useRef(null);

  useEffect(() => {
    if (!active || typeof register !== 'function') return undefined;
    releaseRef.current = register();
    return () => {
      const release = releaseRef.current;
      releaseRef.current = null;
      if (typeof release === 'function') release();
    };
  }, [active, register]);
}

export function useVoice() {
  const ctx = useContext(VoiceContext);
  if (!ctx) {
    // Allow use outside the provider in tests — return an inert snapshot.
    return {
      state: 'off',
      provider: null,
      transcript: '',
      active: false,
      // No provider means no lane has registered, which is the correct
      // answer for a component rendered on its own.
      laneMounted: false,
      registerVoiceLane: () => () => {},
      setProvider: () => {},
      start: () => {},
      stop: () => {},
      toggle: () => {},
    };
  }
  return ctx;
}
