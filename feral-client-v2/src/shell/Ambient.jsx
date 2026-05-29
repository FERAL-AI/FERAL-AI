import React, { useEffect, useRef, useState } from 'react';
import LiveOpsStream from './LiveOpsStream';
import { useSomatic } from '../hooks/useSomatic';

/**
 * Ambient — a quiet, adaptive background layer.
 *
 * Resting state: subtle somatic gradient + grain. No floating orb behind
 * the content — the orb belongs where the user intentionally looks at it
 * (Home hero, voice overlay, chat avatar). Live-ops is the opt-in
 * diagnostic overlay: a faint stream of Brain events — gated to dev
 * builds only so a column of "EVENT text_response" debug rows doesn't
 * leak into the bottom-left corner of the shipped UI.
 *
 * Expand triggers: hover bottom-third, press Cmd-Period, or dispatch the
 * custom `v2:ambient-expand` event. Collapses after 3 s idle.
 */
const COLLAPSE_MS = 3000;

// `import.meta.env.DEV` is true only under `vite dev`; in vitest and in
// the production bundle (vite build) it is false, so the strip stays
// hidden during demos. Kept as a module-level constant so the value is
// captured once and the gate is cheap on every render.
const SHOW_LIVE_OPS = Boolean(import.meta.env && import.meta.env.DEV);

export default function Ambient() {
  const [expanded, setExpanded] = useState(false);
  const somatic = useSomatic();
  const timerRef = useRef(null);

  useEffect(() => {
    const armCollapse = () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => setExpanded(false), COLLAPSE_MS);
    };

    const expand = () => {
      setExpanded(true);
      armCollapse();
    };

    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.code === 'Period') {
        e.preventDefault();
        setExpanded((prev) => !prev);
        armCollapse();
      }
    };
    const onPointer = (e) => {
      const y = e.clientY;
      const h = window.innerHeight || 1;
      if (y / h > 0.72) expand();
    };
    const onCustom = () => expand();

    window.addEventListener('keydown', onKey);
    window.addEventListener('pointermove', onPointer, { passive: true });
    window.addEventListener('v2:ambient-expand', onCustom);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('pointermove', onPointer);
      window.removeEventListener('v2:ambient-expand', onCustom);
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const hue = somatic.cognitiveLoad > 0.7
    ? 'warm'
    : somatic.cognitiveLoad > 0.4
      ? 'neutral'
      : 'cool';

  return (
    <div
      className={`v2-ambient v2-ambient--${hue}${expanded ? ' is-expanded' : ''}`}
      aria-hidden="true"
    >
      <div className="v2-ambient-field" />
      <div className="v2-ambient-grain" />
      {SHOW_LIVE_OPS && (
        <div className="v2-ambient-ops" data-testid="v2-ambient-ops">
          <LiveOpsStream active={expanded} />
        </div>
      )}
    </div>
  );
}
