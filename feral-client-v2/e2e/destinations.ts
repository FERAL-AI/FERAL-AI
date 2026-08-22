/**
 * The shell's destination index, read from the source at run time.
 *
 * `src/shell/navigation.js` exists, by its own header, to kill "a
 * hand-written list that restates another list and goes stale". Two
 * e2e specs then restated it by hand anyway, and the copy in
 * `shell_navigation.spec.ts` was five entries behind when this file was
 * written: it had 23 of 28, missing `/console` (the default landing
 * view), `/jobs`, `/approvals`, `/checkpoints` and `/grants`. Three
 * tests in that file say "every destination", and none of them had ever
 * visited those five.
 *
 * Reading the array out of the source removes the copy. The file is
 * parsed as text rather than imported because it imports `lucide-react`,
 * and pulling a React icon package into the Playwright runner buys
 * nothing any spec needs.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

export type Destination = { to: string; label: string };

export function readDestinations(): Destination[] {
  const src = fs.readFileSync(
    path.join(HERE, '..', 'src', 'shell', 'navigation.js'),
    'utf8',
  );
  const start = src.indexOf('export const DESTINATIONS = [');
  if (start < 0) throw new Error('navigation.js: DESTINATIONS array not found');
  const end = src.indexOf('\n];', start);
  if (end < 0) throw new Error('navigation.js: DESTINATIONS array is unterminated');
  const block = src.slice(start, end);

  const out: Destination[] = [];
  const re = /\{\s*to:\s*'([^']+)',\s*label:\s*'([^']+)'/g;
  let m: RegExpExecArray | null;
  // eslint-disable-next-line no-cond-assign
  while ((m = re.exec(block)) !== null) out.push({ to: m[1], label: m[2] });

  // A regex that silently matched nothing would turn every "every
  // destination" test into a green no-op, which is the exact failure
  // mode this file exists to prevent.
  if (out.length < 20) {
    throw new Error(`navigation.js: parsed only ${out.length} destinations, expected 20+`);
  }
  return out;
}
