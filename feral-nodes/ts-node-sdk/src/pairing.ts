/*
 * Client side of the 6-digit pairing flow (HUP_SPEC.md §4.1).
 * Generates a code, announces it to the brain, polls for the API key,
 * and persists the key to ~/.feral/node-keys/<safe>.key (mode 0600), where
 * <safe> is keyFilename() as specified in HUP_SPEC.md §4.1.
 */

import { promises as fs } from "fs";
import * as os from "os";
import * as path from "path";
import * as http from "http";
import * as https from "https";
import * as crypto from "crypto";
import { URL } from "url";

const KEYS_DIR = path.join(os.homedir(), ".feral", "node-keys");

// Exactly the brain's NodeRegisterPayload node_id class. The Python SDK used
// to test membership with str.isalnum(), which is Unicode-aware, so "café"
// was written verbatim there while this class produced "caf_".
//
// The `u` flag is load-bearing and was missing before. Without it the class
// matches per UTF-16 code unit, so an astral-plane character is a surrogate
// pair and becomes TWO underscores; Python's re matches per code point and
// produces one. "😀" was `__.key` here and `_.key` in Python for that reason
// alone, which is a second, independent way these two disagreed.
const DISALLOWED_IN_KEY_FILENAME = /[^A-Za-z0-9._:-]/gu;
const MAX_NODE_ID_LENGTH = 128;
const DISAMBIGUATOR_LENGTH = 8;

/**
 * Canonical key filename for `nodeId`. See HUP_SPEC.md §4.1 step 5.
 *
 * Mirrored by `key_filename()` in
 * feral-nodes/python-node-sdk/src/feral_node_sdk/pairing.py and pinned by the
 * shared fixture table at feral-nodes/spec-fixtures/node_key_filename.json.
 *
 * The two SDKs each derived this by hand from the spec's prose and got
 * different answers, so a node paired through one silently re-paired under the
 * other: "sensor 01" was sensor_01.key here and sensor01.key in Python.
 *
 * Both old rules also mapped distinct node ids onto one file, which is the
 * worse half. This one replaced disallowed characters, so "a b" and "a_b"
 * shared a_b.key and every 6-character non-ASCII id shared ______.key. The
 * hash suffix is what makes the mapping injective again; replacement alone is
 * still many-to-one.
 *
 * The suffix is applied only when sanitising changed something, so every node
 * id the brain accepts keeps the filename both SDKs already write and no
 * working pairing moves.
 */
export function keyFilename(nodeId: string): string {
  const sanitised = nodeId.replace(DISALLOWED_IN_KEY_FILENAME, "_");
  if (
    sanitised === nodeId
    && nodeId.length >= 1
    && nodeId.length <= MAX_NODE_ID_LENGTH
  ) {
    return `${sanitised}.key`;
  }
  // `sanitised` is always pure ASCII here, because every non-ASCII character is
  // outside the allowed class and has been replaced. That is why slicing by
  // UTF-16 units matches Python's slicing by code points, and why the length
  // test above agrees with Python's len() on the branch that returns early.
  // Truncated before hashing so an over-long node id cannot produce a filename
  // the filesystem refuses; the hash still covers the whole id.
  const digest = crypto.createHash("sha256").update(nodeId, "utf8").digest("hex");
  return `${sanitised.slice(0, MAX_NODE_ID_LENGTH)}-${digest.slice(0, DISAMBIGUATOR_LENGTH)}.key`;
}

/** Absolute path of the key file for `nodeId`. */
export function keyPathForNodeId(nodeId: string): string {
  return path.join(KEYS_DIR, keyFilename(nodeId));
}

function keyPath(nodeId: string): string {
  return keyPathForNodeId(nodeId);
}

export async function loadKey(nodeId: string): Promise<string | null> {
  try {
    const data = await fs.readFile(keyPath(nodeId), "utf8");
    const t = data.trim();
    return t.length > 0 ? t : null;
  } catch {
    return null;
  }
}

export async function saveKey(nodeId: string, apiKey: string): Promise<string> {
  await fs.mkdir(KEYS_DIR, { recursive: true });
  const p = keyPath(nodeId);
  await fs.writeFile(p, apiKey.trim() + "\n", { mode: 0o600 });
  try {
    await fs.chmod(p, 0o600);
  } catch {
    /* ignore */
  }
  return p;
}

export function generateCode(): string {
  // crypto.randomInt, not Math.random. This is a pairing credential: it
  // is the only thing standing between a stranger on the LAN and a
  // registered node. Math.random is a fast non-cryptographic PRNG whose
  // internal state is recoverable from a handful of outputs, so codes
  // minted from it are predictable once an attacker has seen a few --
  // and the Python SDK alongside this one already did it correctly with
  // secrets.randbelow (python-node-sdk/.../pairing.py:96).
  //
  // `crypto` was already imported at the top of this file for the
  // node-id digest, so this was a slip rather than a missing dependency.
  //
  // randomInt is uniform over [0, max) with no modulo bias, which
  // matters here because 1_000_000 does not divide 2**32.
  return crypto.randomInt(1_000_000).toString().padStart(6, "0");
}

function httpBase(brainUrl: string): string {
  let u = brainUrl;
  if (u.startsWith("wss://")) u = "https://" + u.slice("wss://".length);
  else if (u.startsWith("ws://")) u = "http://" + u.slice("ws://".length);
  const idx = u.indexOf("/v1/node");
  if (idx >= 0) u = u.slice(0, idx);
  return u.replace(/\/$/, "");
}

interface RequestOpts {
  method: "GET" | "POST";
  url: string;
  body?: string;
  verifyTls: boolean;
  timeoutMs: number;
}

function request(opts: RequestOpts): Promise<string> {
  return new Promise((resolve, reject) => {
    const parsed = new URL(opts.url);
    const isHttps = parsed.protocol === "https:";
    const lib = isHttps ? https : http;
    const req = lib.request(
      {
        method: opts.method,
        hostname: parsed.hostname,
        port: parsed.port || (isHttps ? 443 : 80),
        path: parsed.pathname + parsed.search,
        headers: {
          "Content-Type": "application/json",
          ...(opts.body ? { "Content-Length": Buffer.byteLength(opts.body) } : {}),
        },
        rejectUnauthorized: opts.verifyTls,
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (c: Buffer) => chunks.push(c));
        res.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
      },
    );
    req.setTimeout(opts.timeoutMs, () => req.destroy(new Error("timeout")));
    req.on("error", reject);
    if (opts.body) req.write(opts.body);
    req.end();
  });
}

export interface PairOptions {
  nodeId: string;
  brainUrl: string;
  name?: string;
  code?: string;
  pollIntervalMs?: number;
  timeoutMs?: number;
  verifyTls?: boolean;
}

export async function pair(opts: PairOptions): Promise<string> {
  const code = opts.code ?? generateCode();
  const base = httpBase(opts.brainUrl);
  const verifyTls = opts.verifyTls !== false;
  const timeoutMs = opts.timeoutMs ?? 300_000;
  const pollIntervalMs = opts.pollIntervalMs ?? 2000;

  // eslint-disable-next-line no-console
  console.log(`\n  FERAL pairing code: ${code.slice(0, 3)} ${code.slice(3)}`);
  // eslint-disable-next-line no-console
  console.log("  → Open FERAL → Settings → Devices → Pair and enter the code.");
  // eslint-disable-next-line no-console
  console.log(`  (Will wait up to ${Math.round(timeoutMs / 1000)}s against ${base})\n`);

  try {
    await request({
      method: "POST",
      url: `${base}/api/devices/pair/announce`,
      body: JSON.stringify({
        code,
        node_id: opts.nodeId,
        name: opts.name ?? opts.nodeId,
      }),
      verifyTls,
      timeoutMs: 5000,
    });
  } catch {
    /* announce is best-effort */
  }

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const body = await request({
        method: "GET",
        url: `${base}/api/devices/pair/status?code=${code}&node_id=${encodeURIComponent(opts.nodeId)}`,
        verifyTls,
        timeoutMs: 5000,
      });
      const data = JSON.parse(body);
      if (data.status === "paired" && typeof data.token === "string") {
        const where = await saveKey(opts.nodeId, data.token);
        // eslint-disable-next-line no-console
        console.log(`  ✓ Paired. API key saved to ${where}`);
        return data.token as string;
      }
    } catch {
      /* ignore + retry */
    }
    await new Promise((r) => setTimeout(r, pollIntervalMs));
  }
  throw new Error("Pairing timed out; ask the user to try again.");
}
