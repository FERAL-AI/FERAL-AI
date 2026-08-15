/**
 * SduiRenderer — v2's recursive SDUI/A2UI tree renderer.
 *
 * Single source of truth for rendering brain-emitted `sdui` payloads +
 * third-party GenUI app surfaces inside the v2 client. Replaces the
 * JSON.stringify wall that used to live on GenUICanvas and the
 * plain-text fallback on proactive toasts.
 *
 * Design choices:
 *   * No heavy dependencies. v2 ships with lucide-react only, so maps
 *     + charts are not mounted inline — they render placeholders
 *     directing the user to the Devices or Ambient pages.
 *   * A2UI parity: Row/Column are aliases for HStack/VStack, List is
 *     implemented, Tabs + Modal work, Checkbox/TextField/Slider/
 *     DateTimeInput/MultipleChoice submit values via the unified
 *     `onAction(action_id, value)` contract.
 *   * Forms capture field values in an internal state object + emit a
 *     single event on submit with `{ values: {...} }` so the brain's
 *     UIEventPayload `value` field carries real data instead of the
 *     null the v1 client used to send.
 *   * Every interactive node accepts an `onAction` callback the parent
 *     threads down from the mount point. Pages that mount the tree
 *     for a third-party app pass `onAction={(action_id, value) =>
 *     socket.sendUiEvent(screen_id, action_id, 'tap', value, app_id)}`.
 *   * No state escapes the tree; the brain is the source of truth for
 *     data_model changes, routed through sdui_patch.
 */

import React, { useCallback, useMemo, useState } from 'react';
import Glass from './Glass';
import StatusDot from './StatusDot';
import EmptyState from './EmptyState';

const STACK_GAP = {
  xs: 4, sm: 6, md: 8, lg: 12, xl: 16,
};

function gapPx(spacing) {
  if (typeof spacing === 'number') return spacing;
  if (typeof spacing === 'string' && STACK_GAP[spacing] != null) return STACK_GAP[spacing];
  return 8;
}

function padPx(p) {
  if (typeof p === 'number') return p;
  if (typeof p === 'string') return STACK_GAP[p] ?? 8;
  return 0;
}

function textStyle(style) {
  switch (style) {
    case 'headline': return { fontSize: 18, fontWeight: 700, letterSpacing: 0.2 };
    case 'subtitle': return { fontSize: 15, fontWeight: 600 };
    case 'caption':  return { fontSize: 11, opacity: 0.7 };
    case 'body':     return { fontSize: 13, lineHeight: 1.45 };
    default:         return { fontSize: 14 };
  }
}

function buttonClass(style) {
  if (style === 'primary')   return 'v2-btn v2-btn--primary';
  if (style === 'danger')    return 'v2-btn v2-btn--danger';
  if (style === 'ghost')     return 'v2-btn v2-btn--ghost';
  if (style === 'secondary') return 'v2-btn v2-btn--secondary';
  return 'v2-btn';
}

/**
 * Primary recursive component.
 *
 * Every node visits this function. Nodes that own interactive state
 * (Form, Tabs, Modal, Checkbox, Slider, TextField, etc.) use local
 * `useState` to hold their ephemeral value, then emit `onAction` on
 * user action with the typed value.
 */
export function SduiNode({ node, onAction, depth = 0 }) {
  if (node == null) return null;
  if (typeof node === 'string' || typeof node === 'number') return <span>{String(node)}</span>;
  if (Array.isArray(node)) {
    return (
      <>
        {node.map((child, i) => (
          <SduiNode key={i} node={child} onAction={onAction} depth={depth + 1} />
        ))}
      </>
    );
  }
  if (typeof node !== 'object') return null;

  const type = String(node.type || '').trim();
  const handleAction = useCallback(
    (action_id, value) => {
      if (!action_id) return;
      if (typeof onAction === 'function') onAction(action_id, value);
    },
    [onAction],
  );

  // Layout primitives -------------------------------------------------

  if (type === 'VStack' || type === 'Column') {
    return (
      <div
        className="v2-sdui-stack v2-sdui-stack--v"
        style={{
          display: 'flex', flexDirection: 'column',
          gap: gapPx(node.spacing),
          padding: padPx(node.padding),
        }}
        data-testid={node.testid || undefined}
      >
        <SduiNode node={node.children || []} onAction={onAction} depth={depth + 1} />
      </div>
    );
  }

  if (type === 'HStack' || type === 'Row') {
    return (
      <div
        className="v2-sdui-stack v2-sdui-stack--h"
        style={{
          display: 'flex', flexDirection: 'row', alignItems: 'center',
          gap: gapPx(node.spacing),
          padding: padPx(node.padding),
        }}
      >
        <SduiNode node={node.children || []} onAction={onAction} depth={depth + 1} />
      </div>
    );
  }

  if (type === 'Spacer') {
    return <div style={{ flex: 1, minHeight: node.height || 0 }} />;
  }

  if (type === 'Divider') {
    return <div className="v2-sdui-divider" style={{ height: 1, width: '100%', opacity: 0.2, background: 'currentColor' }} />;
  }

  // Content primitives ------------------------------------------------

  if (type === 'Text') {
    return (
      <span className="v2-sdui-text" style={{ ...textStyle(node.style), color: node.color || 'inherit' }}>
        {node.value != null ? String(node.value) : ''}
      </span>
    );
  }

  if (type === 'Markdown') {
    // v2 has no markdown parser bundled; render as preformatted-ish text.
    return (
      <div className="v2-sdui-markdown" style={{ whiteSpace: 'pre-wrap', fontSize: 13, lineHeight: 1.5 }}>
        {node.content || node.value || ''}
      </div>
    );
  }

  if (type === 'Image' || type === 'AsyncImage') {
    return (
      <img
        src={node.url}
        alt={node.alt || ''}
        style={{
          maxWidth: '100%',
          height: 'auto',
          borderRadius: node.corner_radius || 8,
          display: 'block',
        }}
        loading="lazy"
      />
    );
  }

  if (type === 'Icon') {
    // v2 doesn't dynamically import icons here; render a small circle w/ label.
    return (
      <span
        aria-label={node.name || 'icon'}
        title={node.name || ''}
        className="v2-sdui-icon"
        style={{
          display: 'inline-block',
          width: node.size || 16, height: node.size || 16,
          borderRadius: '50%',
          background: node.color || 'currentColor',
          opacity: 0.75,
        }}
      />
    );
  }

  if (type === 'Badge') {
    return (
      <span
        className="v2-chip v2-chip--muted"
        style={{
          backgroundColor: node.color || undefined,
          color: node.text_color || undefined,
        }}
      >
        {node.label}
      </span>
    );
  }

  if (type === 'Card') {
    return (
      <Glass level={1} radius="md" padding="md" className="v2-sdui-card">
        <div style={{ display: 'flex', flexDirection: 'column', gap: gapPx(node.spacing || 'sm') }}>
          <SduiNode node={node.children || []} onAction={onAction} depth={depth + 1} />
        </div>
      </Glass>
    );
  }

  if (type === 'MetricCard') {
    return (
      <Glass level={1} radius="md" padding="sm" className="v2-sdui-metric">
        <div className="v2-stat-label">{node.label}</div>
        <div className="v2-stat-value" style={{ color: node.color || undefined }}>
          {node.value}{node.unit ? <span style={{ opacity: 0.6, marginLeft: 4 }}>{node.unit}</span> : null}
        </div>
      </Glass>
    );
  }

  if (type === 'Grid') {
    const cols = Math.max(1, Math.min(6, node.columns || 2));
    return (
      <div
        className="v2-sdui-grid"
        style={{
          display: 'grid',
          gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
          gap: gapPx(node.spacing || 'sm'),
        }}
      >
        <SduiNode node={node.children || []} onAction={onAction} depth={depth + 1} />
      </div>
    );
  }

  if (type === 'ScrollView') {
    return (
      <div
        className="v2-sdui-scroll"
        style={{
          overflowY: 'auto',
          maxHeight: node.max_height || 320,
          display: 'flex', flexDirection: 'column',
          gap: gapPx(node.spacing || 'sm'),
          padding: padPx(node.padding),
        }}
      >
        <SduiNode node={node.children || []} onAction={onAction} depth={depth + 1} />
      </div>
    );
  }

  if (type === 'List') {
    const items = Array.isArray(node.items) ? node.items : [];
    return (
      <div
        className="v2-sdui-list"
        style={{ display: 'flex', flexDirection: 'column', gap: gapPx(node.spacing || 'sm') }}
        data-testid={node.testid || undefined}
      >
        {items.length === 0 ? (
          <EmptyState title={node.empty_title || 'Nothing here yet'} hint={node.empty_hint || ''} />
        ) : (
          items.map((item, i) => (
            <SduiNode key={i} node={item} onAction={onAction} depth={depth + 1} />
          ))
        )}
      </div>
    );
  }

  if (type === 'Tabs') {
    return <TabsNode node={node} onAction={onAction} depth={depth} />;
  }

  if (type === 'Modal') {
    return <ModalNode node={node} onAction={onAction} depth={depth} />;
  }

  if (type === 'Accordion' || type === 'AccordionView') {
    return <AccordionNode node={node} onAction={onAction} depth={depth} />;
  }

  // Interactive primitives -------------------------------------------

  if (type === 'Button') {
    return (
      <button
        type="button"
        className={buttonClass(node.style)}
        onClick={() => handleAction(node.action_id, node.value)}
        data-testid={node.testid || (node.action_id ? `sdui-btn-${node.action_id}` : undefined)}
        disabled={!!node.disabled}
      >
        {node.label || node.value || 'Button'}
      </button>
    );
  }

  if (type === 'Checkbox') {
    return <CheckboxNode node={node} onAction={onAction} />;
  }

  if (type === 'TextField') {
    return <TextFieldNode node={node} onAction={onAction} />;
  }

  if (type === 'Slider') {
    return <SliderNode node={node} onAction={onAction} />;
  }

  if (type === 'DateTimeInput') {
    return <DateTimeInputNode node={node} onAction={onAction} />;
  }

  if (type === 'MultipleChoice') {
    return <MultipleChoiceNode node={node} onAction={onAction} />;
  }

  if (type === 'Form' || type === 'FormView') {
    return <FormNode node={node} onAction={onAction} />;
  }

  if (type === 'ProgressBar') {
    const pct = Math.max(0, Math.min(1, Number(node.value) || 0));
    const now = Math.round(pct * 100);
    // The track was a pair of nested plain divs: visually a progress bar,
    // but to assistive tech two anonymous boxes with no role, no value and
    // no name. The measurement itself is real (it comes off node.value),
    // so aria-valuenow reports it rather than inventing it.
    return (
      <div className="v2-sdui-progress" style={{ width: '100%' }}>
        {node.label ? <div className="v2-stat-label">{node.label}</div> : null}
        <div
          role="progressbar"
          aria-valuenow={now}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuetext={`${now}%`}
          /* aria-label rather than aria-labelledby: this renderer draws
             server-driven trees where the same node can appear more than
             once, so there is no id it can mint that is guaranteed unique
             on the page. */
          aria-label={node.label || 'Progress'}
          style={{ width: '100%', height: 6, borderRadius: 999, background: 'var(--v2-fill-strong)', overflow: 'hidden' }}
        >
          <div style={{ width: `${pct * 100}%`, height: '100%', background: node.color || 'currentColor', transition: 'width 300ms ease' }} />
        </div>
      </div>
    );
  }

  if (type === 'Skeleton') {
    return (
      <div className="v2-sdui-skeleton" aria-hidden="true" style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {Array.from({ length: node.lines || 3 }).map((_, i) => (
          <div
            key={i}
            style={{
              width: `${70 + (i % 3) * 10}%`,
              height: 10,
              borderRadius: 4,
              background: 'var(--v2-fill)',
            }}
          />
        ))}
      </div>
    );
  }

  // Heavy components — rendered as muted placeholders so the tree doesn't crash.
  if (type === 'MapView' || type === 'GraphView' || type === 'Chart' || type === 'ChartView' || type === 'Table' || type === 'TableView' || type === 'WebView' || type === 'VideoPlayer' || type === 'AudioPlayer' || type === 'MediaPlayer' || type === 'CodeBlock') {
    return (
      <div
        className="v2-sdui-placeholder"
        style={{
          padding: 10, borderRadius: 8, opacity: 0.75, fontSize: 12,
          border: '1px dashed var(--v2-hairline-strong)',
        }}
      >
        {String(type)}{node.label ? ` · ${node.label}` : ''}
      </div>
    );
  }

  if (type === 'permission_card' || type === 'tcc_card') {
    return <PermissionOrTccCardNode node={node} kind={type} />;
  }

  if (type === '' || !type) {
    return null;
  }

  return (
    <div className="v2-sdui-unknown" style={{ padding: 6, fontSize: 11, opacity: 0.7, border: '1px dashed var(--v2-state-warn)', borderRadius: 4 }}>
      Unknown SDUI component: {type}
    </div>
  );
}

// ---------------------------------------------------------------------
// Phase 6 (permission_card) + Phase 11 (tcc_card) — structured
// permission denial cards. Shape sourced from the brain's
// `agents/permission_card.py:build_permission_card` and
// `agents/tcc_card.py:build_tcc_card`. The same component renders
// both because they share the same wire shape with a `kind`
// discriminator — iOS deeplinks are tappable in the browser the
// same way as macOS ones (they fail gracefully when the OS can't
// open the scheme).
// ---------------------------------------------------------------------

function PermissionOrTccCardNode({ node, kind }) {
  const isMac = kind === 'tcc_card';
  const title = String(node.title || (isMac ? 'FERAL needs a Mac permission' : 'FERAL needs a permission'));
  const description = String(node.description || '');
  const permissionKey = String(node.permission_key || '');
  const deeplink = isMac
    ? String(node.macos_deeplink || '')
    : String(node.ios_deeplink || '');
  const deeplinkLabel = isMac
    ? String(node.macos_deeplink_label || 'Open System Settings on this Mac')
    : String(node.ios_deeplink_label || 'Open Settings');

  const handleOpen = useCallback(() => {
    if (!deeplink) return;
    try {
      // Browsers honour many `x-apple.systempreferences:` /
      // `app-settings:` URLs natively. When they don't (e.g. iOS
      // PWA fallback), the assignment is a no-op and the user can
      // copy the description text manually.
      window.location.href = deeplink;
    } catch (_err) {
      /* swallow — best-effort */
    }
  }, [deeplink]);

  // A permission card is a call to action, not a taxonomy. The old code
  // hued it blue on macOS and orange elsewhere, which put an off-palette
  // amber on every non-mac client for no semantic reason. Both platforms
  // now use the accent; the platform is already signalled by the icon and
  // the deeplink label.
  const accent = 'var(--v2-accent-ring)';
  const accentBg = 'var(--v2-accent-soft)';

  return (
    <Glass
      level={1}
      radius="md"
      padding="sm"
      className="v2-sdui-permission-card"
      style={{ borderColor: accent, background: accentBg }}
    >
      <div
        style={{
          display: 'flex', alignItems: 'baseline', gap: 8,
          marginBottom: 6,
        }}
      >
        <span aria-hidden style={{ fontSize: 18 }}>{isMac ? '🖥️' : '🔒'}</span>
        <strong style={{ fontSize: 15, lineHeight: 1.3 }}>{title}</strong>
      </div>
      {description && (
        <div style={{ fontSize: 13, opacity: 0.85, lineHeight: 1.45, marginBottom: 8 }}>
          {description}
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        {deeplink ? (
          <button
            type="button"
            className="v2-btn v2-btn--primary"
            onClick={handleOpen}
          >
            {deeplinkLabel}
          </button>
        ) : (
          <span style={{ fontSize: 12, opacity: 0.7 }}>{deeplinkLabel}</span>
        )}
        {permissionKey && (
          <code
            style={{
              fontSize: 11, opacity: 0.55, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 220,
            }}
            title={permissionKey}
          >
            {permissionKey}
          </code>
        )}
      </div>
    </Glass>
  );
}

// ---------------------------------------------------------------------
// Helper sub-components
// ---------------------------------------------------------------------

function TabsNode({ node, onAction, depth }) {
  const items = Array.isArray(node.items) ? node.items : [];
  const [active, setActive] = useState(node.default_tab || (items[0] && items[0].id) || 0);
  return (
    <div className="v2-sdui-tabs">
      <div className="v2-tabs v2-tabs--md" role="tablist">
        {items.map((t, i) => {
          const id = t.id ?? i;
          const isActive = id === active;
          return (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={isActive}
              className={`v2-tab${isActive ? ' is-active' : ''}`}
              onClick={() => { setActive(id); if (t.action_id) onAction?.(t.action_id); }}
            >
              <span className="v2-tab-label">{t.label || id}</span>
            </button>
          );
        })}
      </div>
      <div className="v2-sdui-tabs-panel" style={{ marginTop: 10 }}>
        {items.map((t, i) => {
          const id = t.id ?? i;
          if (id !== active) return null;
          return <SduiNode key={id} node={t.body} onAction={onAction} depth={depth + 1} />;
        })}
      </div>
    </div>
  );
}

function ModalNode({ node, onAction, depth }) {
  // Controlled by `open` prop; when the brain flips open -> true via
  // sdui_patch, the modal shows. Close dispatches the configured
  // `cancel_action_id` (or a default `modal_close`).
  if (!node.open) return null;
  return (
    <div
      className="v2-sdui-modal-backdrop"
      role="dialog"
      aria-modal="true"
      style={{
        position: 'fixed', inset: 0, background: 'var(--v2-scrim)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 100,
      }}
      onClick={() => onAction?.(node.cancel_action_id || 'modal_close')}
    >
      <div
        className="v2-sdui-modal"
        onClick={(e) => e.stopPropagation()}
        style={{
          maxWidth: 480, width: '90%',
          borderRadius: 14, padding: 16,
          // --v2-bg-elev never existed, so only the #181818 fallback ever
          // rendered. The real token is --v2-surface-elev.
          background: 'var(--v2-surface-elev)',
          border: '1px solid var(--v2-hairline)',
        }}
      >
        {node.title ? <div style={{ fontSize: 16, fontWeight: 700, marginBottom: 8 }}>{node.title}</div> : null}
        <SduiNode node={node.body} onAction={onAction} depth={depth + 1} />
      </div>
    </div>
  );
}

function AccordionNode({ node, onAction, depth }) {
  const sections = Array.isArray(node.sections) ? node.sections : [];
  const [openIdx, setOpenIdx] = useState(node.default_open != null ? node.default_open : -1);
  return (
    <div className="v2-sdui-accordion">
      {sections.map((s, i) => {
        const isOpen = i === openIdx;
        return (
          <div key={i} style={{ borderBottom: '1px solid var(--v2-hairline)' }}>
            <button
              type="button"
              onClick={() => setOpenIdx(isOpen ? -1 : i)}
              style={{
                width: '100%', textAlign: 'left', padding: '8px 4px',
                background: 'transparent', border: 'none', cursor: 'pointer',
                fontSize: 13, fontWeight: 600,
              }}
            >
              {s.title}
            </button>
            {isOpen ? (
              <div style={{ padding: '4px 6px 10px' }}>
                <SduiNode node={s.body} onAction={onAction} depth={depth + 1} />
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function CheckboxNode({ node, onAction }) {
  const [checked, setChecked] = useState(!!node.value);
  return (
    <label className="v2-sdui-checkbox" style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => {
          setChecked(e.target.checked);
          if (node.action_id) onAction?.(node.action_id, e.target.checked);
        }}
      />
      <span>{node.label}</span>
    </label>
  );
}

function TextFieldNode({ node, onAction }) {
  const [val, setVal] = useState(node.value ?? '');
  return (
    <div className="v2-sdui-textfield" style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {node.label ? <label className="v2-stat-label">{node.label}</label> : null}
      <input
        type={node.input_type || 'text'}
        value={val}
        placeholder={node.placeholder || ''}
        onChange={(e) => {
          setVal(e.target.value);
          if (node.action_id && node.live) onAction?.(node.action_id, e.target.value);
        }}
        onBlur={() => {
          if (node.action_id && !node.live) onAction?.(node.action_id, val);
        }}
        className="v2-input"
      />
    </div>
  );
}

function SliderNode({ node, onAction }) {
  const min = node.min != null ? Number(node.min) : 0;
  const max = node.max != null ? Number(node.max) : 100;
  const step = node.step != null ? Number(node.step) : 1;
  const [val, setVal] = useState(node.value != null ? Number(node.value) : min);
  return (
    <div className="v2-sdui-slider" style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {node.label ? <label className="v2-stat-label">{node.label}: {val}</label> : null}
      <input
        type="range" min={min} max={max} step={step} value={val}
        onChange={(e) => {
          const v = Number(e.target.value);
          setVal(v);
          if (node.action_id && node.live) onAction?.(node.action_id, v);
        }}
        onMouseUp={() => {
          if (node.action_id && !node.live) onAction?.(node.action_id, val);
        }}
        onTouchEnd={() => {
          if (node.action_id && !node.live) onAction?.(node.action_id, val);
        }}
      />
    </div>
  );
}

function DateTimeInputNode({ node, onAction }) {
  const [val, setVal] = useState(node.value ?? '');
  return (
    <div className="v2-sdui-datetime" style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {node.label ? <label className="v2-stat-label">{node.label}</label> : null}
      <input
        type={node.mode === 'date' ? 'date' : node.mode === 'time' ? 'time' : 'datetime-local'}
        value={val}
        onChange={(e) => {
          setVal(e.target.value);
          if (node.action_id) onAction?.(node.action_id, e.target.value);
        }}
        className="v2-input"
      />
    </div>
  );
}

function MultipleChoiceNode({ node, onAction }) {
  const options = Array.isArray(node.options) ? node.options : [];
  const multi = !!node.multi;
  const [val, setVal] = useState(node.value ?? (multi ? [] : null));
  const toggle = (opt) => {
    let next;
    if (multi) {
      const set = new Set(Array.isArray(val) ? val : []);
      if (set.has(opt)) set.delete(opt); else set.add(opt);
      next = Array.from(set);
    } else {
      next = opt;
    }
    setVal(next);
    if (node.action_id) onAction?.(node.action_id, next);
  };
  const isSelected = (opt) => multi ? (Array.isArray(val) && val.includes(opt)) : (val === opt);
  return (
    <div className="v2-sdui-mc" style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
      {options.map((opt, i) => {
        const id = typeof opt === 'object' ? opt.id : opt;
        const label = typeof opt === 'object' ? opt.label : String(opt);
        return (
          <button
            key={i}
            type="button"
            className={`v2-btn v2-btn--ghost${isSelected(id) ? ' is-active' : ''}`}
            onClick={() => toggle(id)}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}

/**
 * FormNode — gathers field values into a single `values` object and
 * emits one action with `{values}` on submit.
 *
 * Supported field shapes inside `fields`:
 *   { name, type: 'text'|'number'|'checkbox'|'select', label, value, options }
 */
function FormNode({ node, onAction }) {
  const fields = Array.isArray(node.fields) ? node.fields : [];
  const initial = useMemo(() => {
    const out = {};
    for (const f of fields) out[f.name] = f.value ?? (f.type === 'checkbox' ? false : '');
    return out;
  }, [fields]);
  const [values, setValues] = useState(initial);
  const setField = (name, v) => setValues((prev) => ({ ...prev, [name]: v }));

  const submit = (e) => {
    e?.preventDefault?.();
    if (node.action_id) onAction?.(node.action_id, { values });
  };

  return (
    <form onSubmit={submit} className="v2-sdui-form" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {fields.map((f, i) => {
        if (f.type === 'checkbox') {
          return (
            <label key={i} style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
              <input
                type="checkbox"
                checked={!!values[f.name]}
                onChange={(e) => setField(f.name, e.target.checked)}
              />
              <span>{f.label}</span>
            </label>
          );
        }
        if (f.type === 'select') {
          return (
            <label key={i} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              <span className="v2-stat-label">{f.label}</span>
              <select
                value={values[f.name] ?? ''}
                onChange={(e) => setField(f.name, e.target.value)}
                className="v2-btn v2-btn--ghost"
              >
                {(f.options || []).map((o, j) => {
                  const id = typeof o === 'object' ? o.id : o;
                  const label = typeof o === 'object' ? o.label : String(o);
                  return <option key={j} value={id}>{label}</option>;
                })}
              </select>
            </label>
          );
        }
        return (
          <label key={i} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <span className="v2-stat-label">{f.label}</span>
            <input
              type={f.type === 'number' ? 'number' : 'text'}
              value={values[f.name] ?? ''}
              placeholder={f.placeholder || ''}
              onChange={(e) => setField(f.name, e.target.value)}
              className="v2-input"
              data-testid={`sdui-form-field-${f.name}`}
            />
          </label>
        );
      })}
      <button
        type="submit"
        className="v2-btn v2-btn--primary"
        data-testid={node.submit_testid || (node.action_id ? `sdui-form-submit-${node.action_id}` : 'sdui-form-submit')}
      >
        {node.submit_label || 'Submit'}
      </button>
    </form>
  );
}

// ---------------------------------------------------------------------
// Patch protocol
// ---------------------------------------------------------------------

/**
 * Apply a JSON-Patch-like sequence of operations to an SDUI tree.
 *
 * Supported ops (subset of RFC 6902):
 *   - { path: "a/b/0/value", op: "replace", value: ... }
 *   - { path: "a/b", op: "add", value: ... }          (appends for arrays)
 *   - { path: "a/b/0", op: "remove" }
 *
 * Returns a new tree; never mutates the input. Invalid paths are
 * ignored (with a console.warn) so a bad patch from the brain never
 * blanks the entire UI.
 */
export function applySduiPatches(tree, patches) {
  if (!Array.isArray(patches) || patches.length === 0) return tree;
  let next = clone(tree);
  for (const patch of patches) {
    if (!patch || typeof patch !== 'object' || !patch.path) continue;
    const segments = splitPath(patch.path);
    const op = patch.op || 'replace';
    try {
      if (op === 'replace') {
        setAt(next, segments, patch.value);
      } else if (op === 'add') {
        addAt(next, segments, patch.value);
      } else if (op === 'remove') {
        removeAt(next, segments);
      }
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn('applySduiPatches: bad patch', patch, err);
    }
  }
  return next;
}

function clone(v) {
  if (v == null || typeof v !== 'object') return v;
  return JSON.parse(JSON.stringify(v));
}

function splitPath(p) {
  if (typeof p !== 'string' || !p) return [];
  return p.replace(/^\//, '').split('/').map((seg) => {
    if (/^\d+$/.test(seg)) return Number(seg);
    return seg;
  });
}

function walk(tree, segments) {
  let cur = tree;
  for (const seg of segments) {
    if (cur == null) return undefined;
    cur = cur[seg];
  }
  return cur;
}

function setAt(tree, segments, value) {
  if (segments.length === 0) return;
  const parent = walk(tree, segments.slice(0, -1));
  const key = segments[segments.length - 1];
  if (parent == null) return;
  parent[key] = value;
}

function addAt(tree, segments, value) {
  if (segments.length === 0) return;
  const parent = walk(tree, segments.slice(0, -1));
  const key = segments[segments.length - 1];
  if (parent == null) return;
  if (Array.isArray(parent)) {
    if (key === '-' || key === parent.length) parent.push(value);
    else if (typeof key === 'number') parent.splice(key, 0, value);
    else parent[key] = value;
  } else {
    parent[key] = value;
  }
}

function removeAt(tree, segments) {
  if (segments.length === 0) return;
  const parent = walk(tree, segments.slice(0, -1));
  const key = segments[segments.length - 1];
  if (parent == null) return;
  if (Array.isArray(parent) && typeof key === 'number') {
    parent.splice(key, 1);
  } else {
    delete parent[key];
  }
}

// ---------------------------------------------------------------------
// Top-level mount
// ---------------------------------------------------------------------

/**
 * SduiRenderer — the public top-level mount point.
 *
 * Consumers pass a `tree` (the `root` of an SDUIPayload) + `onAction`
 * callback. Trees wrapped in an object with a `type` field are
 * rendered directly; arrays render as fragments.
 */
export default function SduiRenderer({ tree, onAction, className = '' }) {
  if (!tree) return null;
  return (
    <div className={`v2-sdui ${className}`.trim()} data-testid="v2-sdui-root">
      <SduiNode node={tree} onAction={onAction} depth={0} />
    </div>
  );
}
