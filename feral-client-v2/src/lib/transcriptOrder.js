/**
 * Ordered insertion for voice transcript rows.
 *
 * Voice transcripts do not arrive in conversation order. OpenAI's
 * Realtime docs say `conversation.item.input_audio_transcription.completed`
 * "runs asynchronously with Response creation, so this event may come
 * before or after the Response events" — so the user's own words can
 * land *after* the assistant reply that answered them. Chat used to
 * blind-append every transcript, which rendered the question below the
 * answer (operator report 2026-07-28).
 *
 * The brain now stamps ordering metadata on each frame (see
 * `feral-core/voice/transcript_order.py`):
 *
 *   - `itemId`         — provider-stable item identity, also the replace
 *                        key for a late final superseding a partial.
 *   - `previousItemId` — the item this one follows. Chained together
 *                        these links are the provider's canonical order.
 *   - `seq`            — brain-assigned monotonic counter, the fallback
 *                        for providers with no item identity at all.
 *
 * Note that `seq` alone CANNOT fix the race: it is assigned at emit
 * time, so it encodes the same (wrong) arrival order the client already
 * had. Only the item links carry the true order. The decisive rule is
 * the reverse edge — if a row already on screen declares the incoming
 * row as its predecessor, the incoming row must be placed *before* it.
 * That is what pulls a late user transcript back above the reply.
 * `seq` only breaks ties inside the window the links leave open.
 */

/**
 * Insert `row` into `messages`, honouring transcript ordering metadata.
 *
 * Pure: returns a new array and never mutates `messages`. Rows without
 * any ordering metadata (plain chat messages) are left where they are
 * and a metadata-free `row` simply appends, so this is safe to use for
 * every transcript regardless of provider.
 *
 * @param {Array<object>} messages current message list
 * @param {object} row incoming row: { itemId?, previousItemId?, seq?, ... }
 * @returns {Array<object>} new message list
 */
export function insertTranscriptMessage(messages, row) {
  const list = Array.isArray(messages) ? messages : [];

  // A late final carrying the same item id as an earlier partial
  // supersedes it in place — same slot, same React key, no duplicate
  // bubble.
  if (row.itemId) {
    const existing = list.findIndex((m) => m.itemId && m.itemId === row.itemId);
    if (existing >= 0) {
      const next = list.slice();
      next[existing] = { ...row, id: list[existing].id };
      return next;
    }
  }

  // Lower bound: never place the row above its own declared predecessor.
  let lower = 0;
  if (row.previousItemId) {
    const anchor = list.findIndex(
      (m) => m.itemId && m.itemId === row.previousItemId,
    );
    if (anchor >= 0) lower = anchor + 1;
  }

  // Upper bound: any row already on screen that names this row as its
  // predecessor must stay below it. This is the edge that repairs the
  // out-of-order case — the assistant reply arrived first and points
  // back at the user item we are inserting now.
  let upper = list.length;
  if (row.itemId) {
    const successor = list.findIndex(
      (m) => m.previousItemId && m.previousItemId === row.itemId,
    );
    if (successor >= 0 && successor < upper) upper = successor;
  }
  if (upper < lower) upper = lower;

  // Inside the window the links leave open, order by seq. Rows with no
  // seq (plain chat messages, older brains) are transparent here: they
  // keep their position and the voice row settles around them.
  let pos = upper;
  if (typeof row.seq === 'number') {
    for (let i = lower; i < upper; i += 1) {
      const seq = list[i]?.seq;
      if (typeof seq === 'number' && seq > row.seq) {
        pos = i;
        break;
      }
    }
  }

  const next = list.slice();
  next.splice(pos, 0, row);
  return next;
}

/**
 * Normalize a `transcript` frame payload into a chat row.
 *
 * Handles the legacy `[user] ` sentinel some brain builds still emit in
 * the text itself, and maps the wire's snake_case ordering fields onto
 * the camelCase the message list uses.
 *
 * @param {object} payload transcript frame payload
 * @param {string} id id to assign the row
 * @returns {object|null} row, or null when there is nothing to render
 */
export function transcriptRowFromPayload(payload, id) {
  const p = payload || {};
  const role = p.role || (p.text?.startsWith('[user] ') ? 'user' : 'assistant');
  const text = role === 'user' && p.text?.startsWith('[user] ')
    ? p.text.slice(7)
    : (p.text || '');
  if (!text) return null;
  return {
    id,
    role,
    text,
    source: 'voice',
    itemId: p.item_id || null,
    previousItemId: p.previous_item_id || null,
    seq: typeof p.seq === 'number' ? p.seq : null,
  };
}
