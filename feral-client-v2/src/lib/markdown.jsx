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

// Highlight + KaTeX CSS is imported once at the app root (bootstrap.js)
// so it lives in the shared bundle rather than being duplicated each
// time MarkdownMessage mounts. Don't import it here.

const REMARK_PLUGINS = [remarkGfm, remarkMath];
const REHYPE_PLUGINS = [
  [rehypeHighlight, { ignoreMissing: true, detect: true }],
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

const COMPONENTS = {
  a: ExternalLink,
  img: CappedImage,
  // Override <pre> to mark the language so the highlight.js stylesheet
  // can target the chip + scrollable container.
  pre({ node, children, ...rest }) {
    return (
      <pre className="v2-md-pre" {...rest}>
        {children}
      </pre>
    );
  },
  code({ inline, className, children, ...rest }) {
    const lang = /language-(\w+)/.exec(className || '')?.[1];
    if (inline) {
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
