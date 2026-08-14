/**
 * Build the iframe srcdoc that AppSurface uses to render a third-party
 * GenUI surface inside a sandbox.
 *
 * Two halves:
 *   1. Static SDUI → HTML conversion. Only a small subset of node
 *      types are supported here on purpose: this is the surface the
 *      *publisher's manifest* drives, so it has to be predictable
 *      enough to escape correctly. Anything we don't know how to
 *      render is dropped silently — the host page still has the full
 *      SduiRenderer tree available for development tooling.
 *   2. A tiny inline bootstrap that uses addEventListener (NEVER
 *      inline event handlers, NEVER eval) to forward clicks on
 *      `[data-action-id]` nodes to the parent via postMessage in the
 *      AppMessage envelope.
 */

import { buildCspHeader } from './AppSurface.csp.js';

function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function escapeAttr(str) {
  return escapeHtml(str);
}

function renderNode(node) {
  if (node == null) return '';
  if (typeof node === 'string' || typeof node === 'number') {
    return `<span>${escapeHtml(String(node))}</span>`;
  }
  if (Array.isArray(node)) return node.map(renderNode).join('');
  if (typeof node !== 'object') return '';

  const type = node.type || node.componentType || 'Container';
  const children = Array.isArray(node.children) ? node.children.map(renderNode).join('') : '';

  switch (type) {
    case 'Text': {
      const value = escapeHtml(node.value ?? '');
      const style = node.style ? ` data-style="${escapeAttr(node.style)}"` : '';
      return `<p${style}>${value}</p>`;
    }
    case 'Button': {
      const label = escapeHtml(node.label ?? node.value ?? 'Button');
      const actionId = node.action_id ? ` data-action-id="${escapeAttr(node.action_id)}"` : '';
      const style = node.style ? ` data-style="${escapeAttr(node.style)}"` : '';
      return `<button type="button"${actionId}${style}>${label}</button>`;
    }
    case 'Image': {
      const src = node.src || node.value || '';
      if (!src) return '';
      return `<img src="${escapeAttr(src)}" alt="${escapeAttr(node.alt ?? '')}" />`;
    }
    case 'Divider':
      return '<hr />';
    case 'Row':
    case 'HStack':
      return `<div data-layout="row">${children}</div>`;
    case 'Column':
    case 'VStack':
      return `<div data-layout="column">${children}</div>`;
    case 'List':
      return `<ul>${(node.items || []).map((it) => `<li>${renderNode(it)}</li>`).join('')}</ul>`;
    case 'Card':
      return `<section data-card="true">${children}</section>`;
    case 'Container':
    default:
      return `<div data-type="${escapeAttr(type)}">${children}</div>`;
  }
}

export function renderTreeToHtml(tree) {
  if (tree == null) return '';
  return renderNode(tree);
}

const BOOTSTRAP_SCRIPT = `
(function(){
  var ALLOWED = { request_data: 1, submit_form: 1, navigate: 1, close: 1 };
  var keyId = (document.body && document.body.dataset && document.body.dataset.signedKeyId) || 'unsigned';
  function postAction(actionId, value){
    if (!ALLOWED.submit_form) return;
    var msg = {
      type: 'submit_form',
      payload: { action_id: String(actionId), value: value == null ? null : value },
      message_id: 'm-' + Date.now() + '-' + Math.random().toString(36).slice(2,8),
      signed_with_key_id: keyId
    };
    try { window.parent.postMessage(msg, '*'); } catch (e) {}
  }
  document.addEventListener('click', function(ev){
    var t = ev.target;
    while (t && t !== document.body) {
      if (t.dataset && t.dataset.actionId) {
        ev.preventDefault();
        postAction(t.dataset.actionId, null);
        return;
      }
      t = t.parentNode;
    }
  });
})();
`;

// These styles are injected into a sandboxed iframe srcdoc under a strict
// CSP. That document does not load tokens.css, so var(--v2-*) cannot
// resolve here and every colour has to be a literal. Keep the values in
// sync with src/styles/tokens.css by hand.
const BASE_STYLES = `
  :root { color-scheme: dark light; font-family: -apple-system, system-ui, Segoe UI, sans-serif; }
  body { margin: 0; padding: 12px; background: transparent; color: inherit; }
  [data-layout="row"] { display: flex; flex-direction: row; gap: 8px; }
  [data-layout="column"] { display: flex; flex-direction: column; gap: 8px; }
  [data-card="true"] { padding: 12px; border-radius: 12px; background: rgba(255,255,255,0.04); }
  button { cursor: pointer; padding: 6px 12px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.15); background: rgba(255,255,255,0.06); color: inherit; }
  button[data-style="primary"] { background: #0A84FF; border-color: #0A84FF; color: #FFFFFF; }  /* --v2-accent / --v2-on-accent */
  hr { border: 0; border-top: 1px solid rgba(255,255,255,0.1); margin: 8px 0; }
  ul { padding-left: 20px; margin: 0; }
  img { max-width: 100%; }
`;

export function buildSrcDoc({ tree, manifest, signedWithKeyId = 'unsigned' }) {
  const csp = buildCspHeader(manifest);
  const html = renderTreeToHtml(tree);
  return [
    '<!DOCTYPE html>',
    '<html lang="en">',
    '<head>',
    '<meta charset="utf-8" />',
    `<meta http-equiv="Content-Security-Policy" content="${escapeAttr(csp)}" />`,
    '<meta name="referrer" content="no-referrer" />',
    `<style>${BASE_STYLES}</style>`,
    '</head>',
    `<body data-signed-key-id="${escapeAttr(signedWithKeyId)}">`,
    html,
    `<script>${BOOTSTRAP_SCRIPT}</script>`,
    '</body>',
    '</html>',
  ].join('\n');
}
