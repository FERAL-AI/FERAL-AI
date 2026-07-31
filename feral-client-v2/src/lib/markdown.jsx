/**
 * MarkdownMessage — the single chat-side markdown renderer used by Chat,
 * phone ChatPanel, VisionAskPanel, and any other surface that displays
 * assistant prose.
 *
 * Stack (pinned versions in package.json):
 *   - react-markdown 9 (commonmark + HAST pipeline)
 *   - remark-gfm 4 (tables, task lists, strikethrough, autolinks)
 *   - remark-math 6 + rehype-katex 7 (LaTeX inline + block)
 *   - rehype-highlight 7 + highlight.js 11 (Prism-equivalent code highlight)
 *
 * Why a single component:
 *   - Every chat surface used to emit `{m.text}` raw, so triple-backtick
 *     blocks, tables and `**bold**` rendered as literal characters.
 *   - One component means one upgrade path for the highlighter / math
 *     pipeline and one bundle-cost decision (~120 kB gzipped).
 *
 * Sanitization:
 *   - react-markdown 9 already strips dangerous HTML by default
 *     (uses HAST + does NOT enable rehype-raw). We pass NO raw HTML
 *     pipeline so any <script>, <iframe>, on*= attribute, or
 *     javascript: URL in user-supplied text is dropped silently.
 *   - Links are rendered with rel="noopener noreferrer" target="_blank"
 *     so a tab-jack cannot reach window.opener.
 *   - Inline images are allowed (LLMs emit them as part of replies)
 *     but max-height-capped so a huge data URI cannot push the
 *     composer off-screen.
 */
import React, { useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeHighlight from 'rehype-highlight';
import rehypeKatex from 'rehype-katex';
import CopyButton from '../ui/CopyButton';

// Highlight + KaTeX CSS is imported once at the app root (bootstrap.js)
// so it lives in the shared bundle rather than being duplicated each
// time MarkdownMessage mounts. Don't import it here.

const REMARK_PLUGINS = [remarkGfm, remarkMath];
// `detect: false` keeps highlight.js off bare ```fenced``` blocks the
// LLM didn't tag with a language. Auto-detection was painting prose
// deliverables (Slack standups, weekly recaps) orange/yellow by
// classifying English keywords ("and", "on", "in") as code. Language-
// tagged blocks (```python, ```bash, ```ts) still highlight normally.
const REHYPE_PLUGINS = [
  [rehypeHighlight, { ignoreMissing: true, detect: false }],
  rehypeKatex,
];

function ExternalLink({ href, children, ...rest }) {
  const safe = typeof href === 'string' && /^(https?:|mailto:|#|\/)/i.test(href);
  if (!safe) {
    return <span {...rest}>{children}</span>;
  }
  const external = /^https?:/i.test(href);
  return (
    <a
      href={href}
      target={external ? '_blank' : undefined}
      rel={external ? 'noopener noreferrer' : undefined}
      {...rest}
    >
      {children}
    </a>
  );
}

function CappedImage({ src, alt, title }) {
  // Cap to a sane chat-bubble size; preserve aspect by leaving width
  // auto. Strict src filter keeps javascript:/data:text from rendering.
  if (typeof src !== 'string') return null;
  const safe = /^(https?:|data:image\/(png|jpe?g|gif|webp|svg\+xml);)/i.test(src);
  if (!safe) return null;
  return (
    <img
      src={src}
      alt={alt || ''}
      title={title || alt || ''}
      loading="lazy"
      className="v2-md-img"
      style={{
        maxWidth: '100%',
        maxHeight: 360,
        borderRadius: 8,
        display: 'block',
        margin: '6px 0',
      }}
    />
  );
}

/** Flatten a hast subtree back to its source text. */
function hastText(node) {
  if (!node) return '';
  if (node.type === 'text') return node.value || '';
  const kids = Array.isArray(node.children) ? node.children : [];
  let out = '';
  for (const kid of kids) out += hastText(kid);
  return out;
}

/** Pull `language-x` off the hast <code> child of a <pre>. */
function preLanguage(node) {
  const code = Array.isArray(node?.children)
    ? node.children.find((c) => c.tagName === 'code')
    : null;
  const classes = code?.properties?.className;
  const list = Array.isArray(classes) ? classes : String(classes || '').split(/\s+/);
  for (const c of list) {
    const m = /^language-(.+)$/.exec(String(c));
    if (m) return m[1];
  }
  return '';
}

/**
 * Fenced code block with a chrome bar: language label on the left,
 * copy affordance on the right. The raw source is recovered from the
 * hast node (rehype-highlight has already rewritten `children` into
 * span trees, so React children are not usable as clipboard text).
 */
function CodeBlock({ node, children, ...rest }) {
  const lang = preLanguage(node);
  const raw = useMemo(() => hastText(node).replace(/\n$/, ''), [node]);
  return (
    <div className="v2-md-codeblock" data-lang={lang || undefined}>
      <div className="v2-md-codeblock__bar">
        <span className="v2-md-codeblock__lang">{lang || 'plain text'}</span>
        <CopyButton value={raw} label="Copy code" className="v2-md-codeblock__copy" size={12} />
      </div>
      <pre className="v2-md-pre" {...rest}>
        {children}
      </pre>
    </div>
  );
}

const COMPONENTS = {
  a: ExternalLink,
  img: CappedImage,
  // Override <pre> so every fenced block gets a language label + copy
  // button and stays inside its own horizontal scroll container.
  pre: CodeBlock,
  code({ inline, className, children, ...rest }) {
    // react-markdown 9 dropped the `inline` prop. The reliable signal
    // for a fenced (block) code element is the presence of a
    // `language-X` class injected by remark. Anything without that
    // class is inline — i.e. a single-backtick span like `badr`.
    // Treating those as block was painting them with the hljs base
    // palette (light-grey on dark-grey, no border, no padding) which
    // looked like a broken UI badge.
    const lang = /language-(\w+)/.exec(className || '')?.[1];
    const isInline = inline === true || (!lang && !/(^|\s)hljs(\s|$)/.test(className || ''));
    if (isInline) {
      return <code className="v2-md-code-inline" {...rest}>{children}</code>;
    }
    return (
      <code
        className={`hljs ${className || ''}`.trim()}
        data-lang={lang || ''}
        {...rest}
      >
        {children}
      </code>
    );
  },
  table({ children, ...rest }) {
    return (
      <div className="v2-md-table-scroll">
        <table className="v2-md-table" {...rest}>{children}</table>
      </div>
    );
  },
  // Block quotes get a softer left border via CSS.
  blockquote({ children, ...rest }) {
    return <blockquote className="v2-md-blockquote" {...rest}>{children}</blockquote>;
  },
};

/**
 * Render assistant markdown. Accepts a string (typical) or the empty
 * states ('' / null / undefined → null). Trims trailing whitespace so
 * a stream that ends mid-newline doesn't leave a tall gap before the
 * tool-call card.
 */
export default function MarkdownMessage({ text, className = '' }) {
  const trimmed = useMemo(() => (typeof text === 'string' ? text.replace(/\s+$/, '') : ''), [text]);
  if (!trimmed) return null;
  return (
    <div className={`v2-md ${className}`.trim()}>
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={COMPONENTS}
      >
        {trimmed}
      </ReactMarkdown>
    </div>
  );
}
