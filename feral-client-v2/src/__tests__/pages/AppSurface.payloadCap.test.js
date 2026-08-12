/**
 * F-07 — the AppMessage payload cap must mean the same thing on both sides.
 *
 * `MAX_PAYLOAD_BYTES` is 64 KiB here and in `feral-core/genui/app_message_schema.py`.
 * Same name, same number, two different quantities until this test existed:
 *
 *   - This half measured `JSON.stringify(payload).length`, which is UTF-16 code
 *     units. One unit per BMP character, two per emoji.
 *   - Python measured `json.dumps(v).encode("utf-8")` with the stdlib defaults,
 *     which are `ensure_ascii=True` (six bytes per non-ASCII character, twelve
 *     per emoji) and `separators=(', ', ': ')` (two extra bytes per key).
 *
 * Measured before the fix with `{a: "中".repeat(11000)}`: 11008 here, accepted;
 * 66009 in Python, refused. This half is the one an attacker controls, so it
 * was also the loose one: 30000 CJK characters is 90008 bytes of payload and
 * only 30002 UTF-16 units.
 *
 * Both sides now measure UTF-8 bytes of the compact JSON encoding. The fixture
 * is read from the Python tree rather than copied, because a copy is what let
 * these two drift 6x apart in the first place.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import {
  MAX_PAYLOAD_BYTES,
  measurePayloadBytes,
  validateAppMessage,
} from '../../pages/AppSurface.types';

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = join(
  HERE,
  '../../../../feral-core/tests/fixtures/app_message_payload_sizes.json',
);

const FIXTURE = JSON.parse(readFileSync(FIXTURE_PATH, 'utf8'));

/** Materialise a fixture payload. Mirrored line for line in the Python test. */
function build(spec) {
  if (spec.kind === 'literal') return spec.value;
  if (spec.kind === 'repeat') return { [spec.key]: spec.unit.repeat(spec.count) };
  if (spec.kind === 'int_keys') {
    const out = {};
    for (let i = 0; i < spec.count; i += 1) out[`${spec.prefix}${i}`] = i;
    return out;
  }
  throw new Error(`unknown fixture build kind: ${spec.kind}`);
}

function envelope(payload) {
  return {
    type: 'submit_form',
    payload,
    message_id: 'm-1',
    signed_with_key_id: 'k-1',
  };
}

describe('AppMessage payload cap (shared fixture with the brain)', () => {
  it('the fixture cap is this module\'s cap, or every number below lies', () => {
    expect(FIXTURE.max_payload_bytes).toBe(MAX_PAYLOAD_BYTES);
  });

  for (const testCase of FIXTURE.cases) {
    it(`${testCase.name}: measures ${testCase.expected_bytes} UTF-8 bytes`, () => {
      const payload = build(testCase.build);
      expect(measurePayloadBytes(payload), testCase.why).toBe(testCase.expected_bytes);
    });

    it(`${testCase.name}: ${testCase.expected_accepted ? 'accepted' : 'refused'}`, () => {
      const payload = build(testCase.build);
      const result = validateAppMessage(envelope(payload));
      expect(result !== null, testCase.why).toBe(testCase.expected_accepted);
    });
  }

  it('refuses an oversize non-ASCII payload the UTF-16 count waved through', () => {
    // 30000 CJK characters: 30002 UTF-16 units (under the old check) but
    // 90008 bytes. This is the direction that mattered for security.
    const payload = { a: '中'.repeat(30000) };
    expect(JSON.stringify(payload).length).toBeLessThan(MAX_PAYLOAD_BYTES);
    expect(measurePayloadBytes(payload)).toBe(90008);
    expect(validateAppMessage(envelope(payload))).toBeNull();
  });

  it('accepts a payload the brain accepts, at exactly the cap', () => {
    const payload = { a: 'x'.repeat(MAX_PAYLOAD_BYTES - 8) };
    expect(measurePayloadBytes(payload)).toBe(MAX_PAYLOAD_BYTES);
    expect(validateAppMessage(envelope(payload))).not.toBeNull();
  });

  it('refuses one byte over the cap', () => {
    const payload = { a: 'x'.repeat(MAX_PAYLOAD_BYTES - 7) };
    expect(measurePayloadBytes(payload)).toBe(MAX_PAYLOAD_BYTES + 1);
    expect(validateAppMessage(envelope(payload))).toBeNull();
  });

  it('returns null rather than throwing on a payload that cannot be serialised', () => {
    const circular = {};
    circular.self = circular;
    expect(measurePayloadBytes(circular)).toBeNull();
    expect(validateAppMessage(envelope(circular))).toBeNull();
  });
});
