/**
 * useSomatic — derived view of the orb mode + cognitive load.
 *
 * AUDIT-r14 finding 03 dedup: this hook used to spin its own
 * 10s `/api/dashboard` poll, stacking on top of Home.jsx (15s)
 * and GlassBrain.jsx (8s) for ~29 dashboard GETs/min steady-state
 * across the Home + GlassBrain + Shell-somatic combo. It now
 * delegates to the single shared `useSystemHealth` store so the
 * Shell ambient strip, the Home page, the Glass Brain page, and
 * the HubLauncher all subscribe to one 15s tick + one in-flight
 * request. The 5s server cache on `/api/dashboard` (Lane 06)
 * collapses any racey clients further.
 */
import { useSomaticHealth } from './useSystemHealth';

export function useSomatic() {
  return useSomaticHealth();
}
