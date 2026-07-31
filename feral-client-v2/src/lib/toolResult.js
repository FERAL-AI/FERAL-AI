/**
 * toolResult: shape detection + formatting for tool-call arguments
 * and results.
 *
 * The brain emits `tool_start` / `tool_result` frames whose
 * `args_preview` / `result_preview` fields are deliberately untyped:
 * a file read returns source text, a shell call returns stdout, a web
 * search returns an array of hit objects, an HTTP skill returns a JSON
 * body, and a screenshot returns a data URI. Rendering all of those
 * through one `<pre>` is what made the chat read as "unstructured
 * shit".
 *
 * This module is pure (no React) so the classification is unit
 * testable on its own. `ToolResultView` consumes `detectResultShape`
 * and picks a renderer.
 *
 * Nothing here ever invents content: every branch either passes the
 * value through verbatim or reformats it losslessly (JSON re-indent).
 */

// ── Limits ────────────────────────────────────────────────────────
// Guardrails so a 5 MB stdout dump can't lock the main thread inside
// JSON.stringify / regex scans. Anything past the cap renders as
// truncated text with an explicit "truncated" affordance.
export const MAX_SCAN_CHARS = 200_000;
export const MAX_JSON_CHARS = 60_000;
export const MAX_TABLE_ROWS = 200;
export const MAX_TABLE_COLS = 12;

const IMAGE_HTTP_RE = /^https?:\/\/\S+\.(?:png|jpe?g|gif|webp|svg)(?:\?\S*)?$/i;
const IMAGE_DATA_RE = /^data:image\/(?:png|jpe?g|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=\s]+$/i;

const EXT_LANG = {
  js: 'javascript', mjs: 'javascript', cjs: 'javascript', jsx: 'jsx',
  ts: 'typescript', tsx: 'tsx',
  py: 'python', rb: 'ruby', go: 'go', rs: 'rust', java: 'java',
  c: 'c', h: 'c', cc: 'cpp', cpp: 'cpp', hpp: 'cpp', cs: 'csharp',
  swift: 'swift', kt: 'kotlin', php: 'php', lua: 'lua', pl: 'perl',
  sh: 'bash', bash: 'bash', zsh: 'bash', fish: 'bash',
  sql: 'sql', html: 'html', htm: 'html', css: 'css', scss: 'scss',
  json: 'json', yaml: 'yaml', yml: 'yaml', toml: 'toml', ini: 'ini',
  xml: 'xml', md: 'markdown', markdown: 'markdown',
  dockerfile: 'dockerfile', nix: 'nix', diff: 'diff', patch: 'diff',
};

/** Map a file path / filename to a highlight.js language id, or ''. */
export function guessLanguageFromPath(path) {
  if (typeof path !== 'string' || !path.trim()) return '';
  const base = path.trim().split(/[\\/]/).pop() || '';
  if (/^dockerfile$/i.test(base)) return 'dockerfile';
  if (/^makefile$/i.test(base)) return 'makefile';
  const ext = base.includes('.') ? base.split('.').pop().toLowerCase() : '';
  return EXT_LANG[ext] || '';
}

/** True when the string is a renderable image reference. */
export function isImageUrl(value) {
  if (typeof value !== 'string') return false;
  const v = value.trim();
  if (v.length > MAX_SCAN_CHARS) return false;
  return IMAGE_HTTP_RE.test(v) || IMAGE_DATA_RE.test(v);
}

/**
 * Parse a JSON *object or array* out of a string. Returns undefined
 * for anything else. We never want `"42"` or `"true"` promoted out of
 * their string form, because tools return those as literal output.
 */
export function tryParseJson(text) {
  if (typeof text !== 'string') return undefined;
  const t = text.trim();
  if (!t || t.length > MAX_SCAN_CHARS) return undefined;
  const head = t[0];
  if (head !== '{' && head !== '[') return undefined;
  try {
    const parsed = JSON.parse(t);
    return parsed && typeof parsed === 'object' ? parsed : undefined;
  } catch {
    return undefined;
  }
}

/** Unified-diff / git-diff detection. */
export function looksLikeDiff(value) {
  if (typeof value !== 'string' || value.length > MAX_SCAN_CHARS) return false;
  if (/^diff --git /m.test(value)) return true;
  return /^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@/m.test(value);
}

/** Fenced-code-block detection (assistant-style markdown in a result). */
export function looksLikeMarkdown(value) {
  if (typeof value !== 'string' || value.length > MAX_SCAN_CHARS) return false;
  return /^```/m.test(value);
}

const SCALAR = new Set(['string', 'number', 'boolean']);
function isScalar(v) {
  return v == null || SCALAR.has(typeof v);
}

/**
 * Coerce an array of like-shaped objects into `{ columns, rows }`.
 * Returns null when the value isn't tabular enough to be worth a
 * table (mixed shapes, deeply nested cells, too many columns).
 */
export function toTable(value) {
  if (!Array.isArray(value) || value.length === 0) return null;
  if (value.length > MAX_TABLE_ROWS) return null;
  if (!value.every((r) => r && typeof r === 'object' && !Array.isArray(r))) return null;

  const columns = [];
  for (const row of value) {
    for (const key of Object.keys(row)) {
      if (!columns.includes(key)) columns.push(key);
      if (columns.length > MAX_TABLE_COLS) return null;
    }
  }
  if (columns.length === 0) return null;

  let scalarCells = 0;
  const totalCells = value.length * columns.length;
  for (const row of value) {
    for (const col of columns) {
      if (isScalar(row[col])) scalarCells += 1;
    }
  }
  // Below 80% scalar the "table" is really nested JSON wearing a hat.
  if (scalarCells / totalCells < 0.8) return null;
  return { columns, rows: value };
}

// Common envelope keys tools wrap their row lists in.
const TABLE_ENVELOPE_KEYS = ['rows', 'results', 'items', 'records', 'entries', 'data', 'hits'];

/** Look one level into an object for an embedded row list. */
export function unwrapTable(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  for (const key of TABLE_ENVELOPE_KEYS) {
    const table = toTable(value[key]);
    if (table) return { ...table, envelope: key };
  }
  return null;
}

const IMAGE_FIELDS = ['image_url', 'image', 'url', 'src', 'screenshot', 'data_uri'];

function findImageField(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
  for (const key of IMAGE_FIELDS) {
    if (isImageUrl(value[key])) return String(value[key]);
  }
  return '';
}

/** Pretty-print a value for a details pane. Lossless, size-capped. */
export function formatJson(value) {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try {
    const out = JSON.stringify(value, null, 2);
    if (typeof out !== 'string') return String(value);
    return out.length > MAX_JSON_CHARS
      ? `${out.slice(0, MAX_JSON_CHARS)}\n… (${out.length - MAX_JSON_CHARS} more characters)`
      : out;
  } catch {
    return String(value);
  }
}

/**
 * Pretty-print arguments. Strings that are really JSON (the brain
 * ships `args_preview` as a pre-serialized string) get re-indented so
 * the details pane shows a readable object instead of one long line.
 */
export function formatArgs(value) {
  if (value == null) return '';
  if (typeof value === 'string') {
    const parsed = tryParseJson(value);
    return parsed === undefined ? value : formatJson(parsed);
  }
  return formatJson(value);
}

/** Human duration for a latency figure. `0`/invalid → ''. */
export function formatDuration(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n <= 0) return '';
  if (n < 1000) return `${Math.round(n)}ms`;
  if (n < 60_000) return `${(n / 1000).toFixed(n < 10_000 ? 2 : 1)}s`;
  const mins = Math.floor(n / 60_000);
  const secs = Math.round((n % 60_000) / 1000);
  return `${mins}m ${secs}s`;
}

function collapseWhitespace(text) {
  return String(text).replace(/\s+/g, ' ').trim();
}

/**
 * One-line summary of a tool's arguments for the collapsed card head.
 * Prefers the "interesting" scalar fields (path, query, command, url)
 * so `Read file` reads as `src/App.jsx` rather than `{"path": …}`.
 */
const PREFERRED_ARG_KEYS = [
  'path', 'file_path', 'file', 'filename', 'command', 'cmd', 'script',
  'query', 'q', 'search', 'prompt', 'url', 'endpoint', 'text', 'pattern',
  'name', 'id', 'city', 'location',
];

export function summariseArgs(value, max = 96) {
  if (value == null) return '';
  let subject = value;
  if (typeof subject === 'string') {
    const parsed = tryParseJson(subject);
    if (parsed === undefined) return truncateOneLine(subject, max);
    subject = parsed;
  }
  if (Array.isArray(subject)) {
    return truncateOneLine(`${subject.length} item${subject.length === 1 ? '' : 's'}`, max);
  }
  if (typeof subject !== 'object') return truncateOneLine(String(subject), max);

  const keys = Object.keys(subject);
  if (keys.length === 0) return '';
  for (const key of PREFERRED_ARG_KEYS) {
    if (isScalar(subject[key]) && subject[key] != null && subject[key] !== '') {
      return truncateOneLine(String(subject[key]), max);
    }
  }
  const parts = [];
  for (const key of keys) {
    const v = subject[key];
    if (!isScalar(v) || v == null || v === '') continue;
    parts.push(`${key}: ${String(v)}`);
    if (parts.join(', ').length >= max) break;
  }
  if (parts.length === 0) return truncateOneLine(`${keys.length} field${keys.length === 1 ? '' : 's'}`, max);
  return truncateOneLine(parts.join(', '), max);
}

export function truncateOneLine(text, max = 96) {
  const flat = collapseWhitespace(text);
  if (flat.length <= max) return flat;
  return `${flat.slice(0, Math.max(0, max - 1))}…`;
}

/**
 * Classify a result payload.
 *
 * @returns {{kind: string, data: any, language: string, envelope?: string}}
 *   kind ∈ empty | image | table | code | markdown | json | text
 */
export function detectResultShape(value, opts = {}) {
  const language = typeof opts.language === 'string' ? opts.language : '';

  if (value == null) return { kind: 'empty', data: null, language: '' };

  if (typeof value === 'number' || typeof value === 'boolean') {
    return { kind: 'text', data: String(value), language: '' };
  }

  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed) return { kind: 'empty', data: null, language: '' };
    if (isImageUrl(trimmed)) return { kind: 'image', data: trimmed, language: '' };
    if (looksLikeDiff(value)) return { kind: 'code', data: value, language: 'diff' };
    const parsed = tryParseJson(value);
    if (parsed !== undefined) return detectResultShape(parsed, opts);
    if (looksLikeMarkdown(value)) return { kind: 'markdown', data: value, language: '' };
    if (language) return { kind: 'code', data: value, language };
    return { kind: 'text', data: value, language: '' };
  }

  if (typeof value === 'object') {
    const img = findImageField(value);
    if (img) return { kind: 'image', data: img, language: '' };
    const table = toTable(value);
    if (table) return { kind: 'table', data: table, language: '' };
    const wrapped = unwrapTable(value);
    if (wrapped) return { kind: 'table', data: wrapped, language: '', envelope: wrapped.envelope };
    return { kind: 'json', data: value, language: 'json' };
  }

  return { kind: 'text', data: String(value), language: '' };
}

/** Cell → display string. Objects collapse to compact JSON. */
export function cellText(value) {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  if (SCALAR.has(typeof value)) return String(value);
  try {
    return truncateOneLine(JSON.stringify(value), 80);
  } catch {
    return String(value);
  }
}

/**
 * Aggregate status for a group of tool calls in one turn.
 * `running` wins over `failed` wins over `ok` for the group icon.
 */
export function summariseGroup(traces) {
  const list = Array.isArray(traces) ? traces : [];
  let running = 0;
  let failed = 0;
  let ok = 0;
  let totalMs = 0;
  for (const t of list) {
    if (!t) continue;
    if (t.success === false) failed += 1;
    else if (t.success === true) ok += 1;
    else running += 1;
    totalMs += Number(t.latency_ms) || 0;
  }
  return {
    total: list.length,
    running,
    failed,
    ok,
    totalMs,
    status: running > 0 ? 'running' : failed > 0 ? 'failed' : 'ok',
  };
}
