/**
 * Mirror of feral-core/genui/app_message_schema.py.
 *
 * Keep in lockstep with the Python module — there's a comment in
 * both halves reminding maintainers. The Python side is the
 * authoritative schema for backend parsers; this TS half is what
 * runs on the host (FERAL client) to drop malformed iframe→host
 * postMessage events before they reach the FERAL reducer.
 */

export const APP_MESSAGE_TYPES = [
  'request_data',
  'submit_form',
  'navigate',
  'close',
] as const;

export type AppMessageType = (typeof APP_MESSAGE_TYPES)[number];

export interface AppMessage {
  type: AppMessageType;
  payload: Record<string, unknown>;
  message_id: string;
  signed_with_key_id: string;
}

export const MAX_PAYLOAD_BYTES = 64 * 1024;

const MAX_ID_LENGTH = 128;

const PAYLOAD_ENCODER = new TextEncoder();

/**
 * Size of `payload` in UTF-8 bytes of its compact JSON encoding, or `null`
 * if it cannot be serialised at all.
 *
 * This is the *one* quantity MAX_PAYLOAD_BYTES is measured in, and it is
 * mirrored by `payload_size_bytes()` in feral-core/genui/app_message_schema.py.
 *
 * This half used to compare `JSON.stringify(payload).length`, which is UTF-16
 * code units: one per BMP character, two per emoji. The brain counted UTF-8
 * bytes of an `ensure_ascii=True` encoding, which is six bytes per BMP
 * character and twelve per emoji. `{a: "中".repeat(11000)}` was 11008 here and
 * 66009 there, so the brain refused payloads this guard had already approved.
 *
 * The gap ran the dangerous way too: 30000 CJK characters is 90008 bytes of
 * payload and only 30002 UTF-16 units, so an oversize payload passed the guard
 * whose stated job is to stop the iframe flooding the host channel.
 *
 * TextEncoder never sees a lone surrogate here: JSON.stringify has been
 * well-formed since ES2019 and escapes them to six ASCII characters, which is
 * what the Python side's `errors="backslashreplace"` produces.
 */
export function measurePayloadBytes(payload: unknown): number | null {
  let serialised: string;
  try {
    serialised = JSON.stringify(payload);
  } catch {
    return null;
  }
  if (typeof serialised !== 'string') return null;
  return PAYLOAD_ENCODER.encode(serialised).length;
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return Boolean(v) && typeof v === 'object' && !Array.isArray(v);
}

function isAllowedType(v: unknown): v is AppMessageType {
  return typeof v === 'string' && (APP_MESSAGE_TYPES as readonly string[]).includes(v);
}

/**
 * Validate an inbound postMessage payload against the strict
 * AppMessage shape. Returns the typed message on success, or
 * `null` for any malformed input. NEVER throws — the host's
 * `window.message` listener uses this as a guard to drop bad
 * events without crashing the message loop.
 */
export function validateAppMessage(raw: unknown): AppMessage | null {
  if (!isPlainObject(raw)) return null;

  const { type, payload, message_id, signed_with_key_id } = raw as Record<string, unknown>;

  if (!isAllowedType(type)) return null;
  if (!isPlainObject(payload)) return null;
  if (typeof message_id !== 'string' || !message_id || message_id.length > MAX_ID_LENGTH) {
    return null;
  }
  if (
    typeof signed_with_key_id !== 'string'
    || !signed_with_key_id
    || signed_with_key_id.length > MAX_ID_LENGTH
  ) {
    return null;
  }

  const payloadBytes = measurePayloadBytes(payload);
  if (payloadBytes === null || payloadBytes > MAX_PAYLOAD_BYTES) return null;

  // Reject unknown top-level keys to mirror Python's `extra="forbid"`.
  const allowedKeys = new Set(['type', 'payload', 'message_id', 'signed_with_key_id']);
  for (const key of Object.keys(raw as Record<string, unknown>)) {
    if (!allowedKeys.has(key)) return null;
  }

  return {
    type,
    payload: payload as Record<string, unknown>,
    message_id,
    signed_with_key_id,
  };
}
