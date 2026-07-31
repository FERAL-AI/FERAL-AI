/**
 * toolResult classification contract.
 *
 * These are the rules ToolResultView leans on to decide whether a
 * payload is code, a table, an image, JSON or plain text. Getting this
 * wrong is what made every tool result look identical, so pin it.
 */
import { describe, it, expect } from 'vitest';
import {
  cellText,
  detectResultShape,
  formatArgs,
  formatDuration,
  formatJson,
  guessLanguageFromPath,
  isImageUrl,
  looksLikeDiff,
  looksLikeMarkdown,
  summariseArgs,
  summariseGroup,
  toTable,
  truncateOneLine,
  tryParseJson,
  unwrapTable,
} from '../../lib/toolResult';

describe('guessLanguageFromPath', () => {
  it('maps common extensions', () => {
    expect(guessLanguageFromPath('src/App.jsx')).toBe('jsx');
    expect(guessLanguageFromPath('/tmp/run.py')).toBe('python');
    expect(guessLanguageFromPath('deploy.sh')).toBe('bash');
    expect(guessLanguageFromPath('Dockerfile')).toBe('dockerfile');
  });
  it('returns empty for unknown or missing paths', () => {
    expect(guessLanguageFromPath('')).toBe('');
    expect(guessLanguageFromPath(null)).toBe('');
    expect(guessLanguageFromPath('notes.zzz')).toBe('');
  });
});

describe('isImageUrl', () => {
  it('accepts http image urls and image data URIs', () => {
    expect(isImageUrl('https://example.com/a.png')).toBe(true);
    expect(isImageUrl('https://example.com/a.jpeg?x=1')).toBe(true);
    expect(isImageUrl('data:image/png;base64,iVBORw0KGgo=')).toBe(true);
  });
  it('rejects non-images and hostile schemes', () => {
    expect(isImageUrl('https://example.com/index.html')).toBe(false);
    expect(isImageUrl('javascript:alert(1)')).toBe(false);
    expect(isImageUrl('data:text/html;base64,AAA')).toBe(false);
    expect(isImageUrl(42)).toBe(false);
  });
});

describe('tryParseJson', () => {
  it('parses objects and arrays only', () => {
    expect(tryParseJson('{"a":1}')).toEqual({ a: 1 });
    expect(tryParseJson('[1,2]')).toEqual([1, 2]);
  });
  it('leaves scalars and junk alone', () => {
    expect(tryParseJson('42')).toBeUndefined();
    expect(tryParseJson('true')).toBeUndefined();
    expect(tryParseJson('not json')).toBeUndefined();
    expect(tryParseJson('{oops')).toBeUndefined();
    expect(tryParseJson(null)).toBeUndefined();
  });
});

describe('diff + markdown sniffing', () => {
  it('detects unified and git diffs', () => {
    expect(looksLikeDiff('diff --git a/x b/x\nindex 1..2')).toBe(true);
    expect(looksLikeDiff('@@ -1,3 +1,4 @@\n-a\n+b')).toBe(true);
    expect(looksLikeDiff('just some prose')).toBe(false);
  });
  it('detects fenced markdown', () => {
    expect(looksLikeMarkdown('intro\n```js\nx\n```')).toBe(true);
    expect(looksLikeMarkdown('no fences here')).toBe(false);
  });
});

describe('toTable / unwrapTable', () => {
  it('builds columns from a uniform row list', () => {
    const t = toTable([{ a: 1, b: 'x' }, { a: 2, b: 'y' }]);
    expect(t.columns).toEqual(['a', 'b']);
    expect(t.rows).toHaveLength(2);
  });
  it('unions keys across rows', () => {
    expect(toTable([{ a: 1 }, { b: 2 }]).columns).toEqual(['a', 'b']);
  });
  it('refuses non-tabular input', () => {
    expect(toTable([])).toBeNull();
    expect(toTable('nope')).toBeNull();
    expect(toTable([1, 2, 3])).toBeNull();
    // Mostly-nested cells are JSON, not a table.
    expect(toTable([{ a: { deep: 1 } }, { a: { deep: 2 } }])).toBeNull();
  });
  it('finds a row list one level down', () => {
    const t = unwrapTable({ results: [{ title: 'a', url: 'b' }] });
    expect(t.envelope).toBe('results');
    expect(t.columns).toEqual(['title', 'url']);
    expect(unwrapTable({ nothing: 1 })).toBeNull();
    expect(unwrapTable([1])).toBeNull();
  });
});

describe('detectResultShape', () => {
  it('classifies empty payloads', () => {
    expect(detectResultShape(null).kind).toBe('empty');
    expect(detectResultShape('   ').kind).toBe('empty');
  });
  it('classifies images from strings and object fields', () => {
    expect(detectResultShape('https://x/y.png').kind).toBe('image');
    expect(detectResultShape({ image_url: 'https://x/y.png' })).toMatchObject({
      kind: 'image', data: 'https://x/y.png',
    });
  });
  it('classifies diffs as code with the diff language', () => {
    const s = detectResultShape('@@ -1 +1 @@\n-a\n+b');
    expect(s.kind).toBe('code');
    expect(s.language).toBe('diff');
  });
  it('parses JSON-shaped strings before classifying', () => {
    expect(detectResultShape('[{"a":1},{"a":2}]').kind).toBe('table');
    expect(detectResultShape('{"a":1}').kind).toBe('json');
  });
  it('uses the caller language hint for plain source text', () => {
    const s = detectResultShape('def f():\n    return 1', { language: 'python' });
    expect(s).toMatchObject({ kind: 'code', language: 'python' });
  });
  it('falls back to text for untyped stdout', () => {
    expect(detectResultShape('build ok\n2 warnings').kind).toBe('text');
    expect(detectResultShape(7).kind).toBe('text');
    expect(detectResultShape(true)).toMatchObject({ kind: 'text', data: 'true' });
  });
  it('routes fenced markdown to the markdown renderer', () => {
    expect(detectResultShape('see:\n```js\n1\n```').kind).toBe('markdown');
  });
});

describe('formatting helpers', () => {
  it('pretty-prints JSON and passes strings through', () => {
    expect(formatJson({ a: 1 })).toBe('{\n  "a": 1\n}');
    expect(formatJson('raw')).toBe('raw');
    expect(formatJson(null)).toBe('');
  });
  it('re-indents JSON-shaped arg strings', () => {
    expect(formatArgs('{"a":1}')).toBe('{\n  "a": 1\n}');
    expect(formatArgs('plain')).toBe('plain');
    expect(formatArgs(null)).toBe('');
  });
  it('formats durations across magnitudes', () => {
    expect(formatDuration(0)).toBe('');
    expect(formatDuration(-1)).toBe('');
    expect(formatDuration(NaN)).toBe('');
    expect(formatDuration(430)).toBe('430ms');
    expect(formatDuration(1500)).toBe('1.50s');
    expect(formatDuration(65_000)).toBe('1m 5s');
  });
  it('truncates to one line', () => {
    expect(truncateOneLine('a\n b   c')).toBe('a b c');
    expect(truncateOneLine('abcdef', 4)).toBe('abc…');
  });
  it('renders cells as scalars, collapsing objects', () => {
    expect(cellText(null)).toBe('');
    expect(cellText('x')).toBe('x');
    expect(cellText(3)).toBe('3');
    expect(cellText({ a: 1 })).toBe('{"a":1}');
  });
});

describe('summariseArgs', () => {
  it('prefers the meaningful key', () => {
    expect(summariseArgs({ limit: 10, path: 'src/App.jsx' })).toBe('src/App.jsx');
    expect(summariseArgs('{"q":"tahoe notes"}')).toBe('tahoe notes');
  });
  it('falls back to key: value pairs', () => {
    expect(summariseArgs({ alpha: 1, beta: 2 })).toBe('alpha: 1, beta: 2');
  });
  it('handles arrays, scalars and empties', () => {
    expect(summariseArgs([1, 2, 3])).toBe('3 items');
    expect(summariseArgs('just a string')).toBe('just a string');
    expect(summariseArgs({})).toBe('');
    expect(summariseArgs(null)).toBe('');
    expect(summariseArgs({ a: { deep: 1 } })).toBe('1 field');
  });
});

describe('summariseGroup', () => {
  it('aggregates counts, duration and a worst-case status', () => {
    const s = summariseGroup([
      { success: true, latency_ms: 100 },
      { success: false, latency_ms: 50 },
      { success: null },
    ]);
    expect(s).toMatchObject({ total: 3, ok: 1, failed: 1, running: 1, totalMs: 150, status: 'running' });
    expect(summariseGroup([{ success: true }, { success: false }]).status).toBe('failed');
    expect(summariseGroup([{ success: true }]).status).toBe('ok');
    expect(summariseGroup(null).total).toBe(0);
  });
});
