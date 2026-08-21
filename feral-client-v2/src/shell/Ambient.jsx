import React, { useEffect, useRef, useState } from 'react';
import { useSomatic } from '../hooks/useSomatic';

/**
 * Ambient — a quiet, adaptive background layer.
 *
 * Resting state: subtle somatic gradient + grain. No floating orb behind
 * the content: the orb belongs where the user intentionally looks at it
 * (Home hero, voice overlay, chat avatar).
 *
 * There is deliberately no event overlay here any more. The old
 * <LiveOpsStream> child was deleted in 2026.8.12: it was gated behind
 * `import.meta.env.DEV` so it never reached a user, and it could not be
 * un-gated as written. Twelve of its thirteen event labels named frame
 * types the brain does not emit (only `tool_result` is real), so the
 * traffic it actually received rendered as the "EVENT text_response"
 * debug rows that CHANGELOG 2026.5.x gated it for; its second `hop`
 * branch tested for `"system"`, which `FeralMessage.hop`
 * (`Literal["client","brain","daemon","skill"]`) can never be; and it
 * rendered aria-hidden inside this aria-hidden layer, so nothing it
 * showed was reachable by assistive tech. The operator-facing activity
 * feeds are the routed /timeline and /oversight pages.
 *
 * Expand triggers: hover bottom-third, press Cmd-Period, or dispatch the
 * custom `v2:ambient-expand` event. Collapses after 3 s idle.
 */
const COLLAPSE_MS = 3000;

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
    </div>
  );
}
