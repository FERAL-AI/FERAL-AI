# Third-Party Notices

This file records third-party material redistributed inside feral-core.

---

## browser-use / browser-harness

**Used in:** `memory/browser_domain_seeds.py` — the bundled reference notes
seeded into the per-domain browser knowledge store
(`memory/browser_domain_memory.py`).

**What was taken:** the interaction-skill topic set (shadow DOM, iframes,
cross-origin iframes, dialogs, dropdowns, uploads, drag-and-drop, scrolling,
screenshots/coordinates, tabs, network requests) and verbatim excerpts from
`interaction-skills/*.md`. Each excerpt is stored in the `upstream_excerpt`
field of its note, alongside an `attribution` field naming the source file and
a `license` field set to `MIT`, so the attribution travels with any copy of
the database.

The `body` field of each note is feral-core's own text, mapping the technique
onto FERAL's browser endpoints. Those bodies are marked `source="feral-core"`
and are **not** attributed to Browser Use.

**License:**

```
MIT License

Copyright (c) 2026 Browser Use

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
