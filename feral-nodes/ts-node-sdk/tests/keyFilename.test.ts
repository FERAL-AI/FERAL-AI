/*
 * F-08 — both node SDKs must name a node's key file identically.
 *
 * Both SDKs persist the pairing token to ~/.feral/node-keys/<safe>.key, and
 * both derived <safe> by hand from prose in HUP_SPEC.md. They landed on
 * different algorithms, so the same node paired through one SDK and then run
 * through the other silently re-paired:
 *
 *   node_id        python (before)   typescript (before)
 *   "sensor 01"    sensor01.key      sensor_01.key
 *   "café"         café.key          caf_.key
 *   "!!!"          .key              ___.key
 *
 * This half replaced disallowed characters with "_" against an ASCII-only
 * class; Python dropped them and its str.isalnum() is Unicode-aware. And
 * HUP_SPEC.md §4.1 documented the path as <node_id>.key with no sanitisation
 * at all, so there were three specifications, not two.
 *
 * Both collapse distinct node ids onto one file. This half mapped "a b" and
 * "a_b" both onto a_b.key, and every 6-character non-ASCII id onto ______.key.
 *
 * The fixture is read from feral-nodes/spec-fixtures rather than copied,
 * because a copy is what let these two drift apart in the first place.
 */

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve, sep } from "node:path";
import * as os from "os";

import * as pairing from "../src/pairing";

/**
 * Call the module's key-path function, or fail saying it is not reachable.
 *
 * Resolved at call time rather than imported by name on purpose: a missing
 * export would make every case below fail with the same TypeError, which
 * proves only that a symbol is absent. The fixture cases have to fail on the
 * filenames themselves for this file to be evidence of anything.
 */
function keyPathForNodeId(nodeId: string): string {
  const fn = (pairing as Record<string, unknown>).keyPathForNodeId;
  if (typeof fn !== "function") {
    throw new Error(
      "src/pairing.ts exports no keyPathForNodeId(), so the filename rule " +
        "cannot be asserted against the Python SDK's. That is how the two " +
        "ended up writing different files for the same node.",
    );
  }
  return (fn as (id: string) => string)(nodeId);
}

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = join(HERE, "../../spec-fixtures/node_key_filename.json");
const SPEC_PATH = join(HERE, "../../HUP_SPEC.md");
const PY_MIRROR = join(
  HERE,
  "../../python-node-sdk/tests/test_key_filename_canonical.py",
);

interface FixtureCase {
  name: string;
  node_id: string;
  filename: string;
  before_python: string;
  before_ts: string;
  why: string;
}

const FIXTURE: { cases: FixtureCase[] } = JSON.parse(
  readFileSync(FIXTURE_PATH, "utf8"),
);

const KEYS_DIR = join(os.homedir(), ".feral", "node-keys");

/** The filename component the SDK gives this node id. */
function filenameFor(nodeId: string): string {
  const full = keyPathForNodeId(nodeId);
  return full.slice(full.lastIndexOf(sep) + 1);
}

describe("node key filename (shared fixture with the Python SDK)", () => {
  for (const testCase of FIXTURE.cases) {
    it(`${testCase.name}: ${JSON.stringify(testCase.node_id)}`, () => {
      expect(filenameFor(testCase.node_id), testCase.why).toBe(testCase.filename);
    });
  }

  it("gives every fixture node id its own file", () => {
    // The point of the algorithm. Both old rules failed this: this half mapped
    // "a b" and "a_b" both onto a_b.key.
    const seen = new Map<string, string>();
    for (const testCase of FIXTURE.cases) {
      const name = filenameFor(testCase.node_id);
      expect(
        seen.has(name),
        `${JSON.stringify(testCase.node_id)} and ${JSON.stringify(seen.get(name))} both resolve to ${name}`,
      ).toBe(false);
      seen.set(name, testCase.node_id);
    }
  });

  it("never escapes the keys directory", () => {
    for (const testCase of FIXTURE.cases) {
      const full = resolve(keyPathForNodeId(testCase.node_id));
      expect(dirname(full), testCase.name).toBe(resolve(KEYS_DIR));
    }
  });

  it("keeps the filename already-legal node ids have today", () => {
    // Compatibility guard: anything the brain accepts
    // (^[A-Za-z0-9._:-]{1,128}$) must keep the exact name both SDKs already
    // write, or this fix would silently unpair working hardware. Compared
    // against the fixture's measured pre-fix names, not against a restatement
    // of the new rule.
    const unchanged = FIXTURE.cases.filter(
      (c) => c.before_python === c.before_ts && c.before_ts === c.filename,
    );
    expect(unchanged.length).toBeGreaterThanOrEqual(4);
    for (const testCase of unchanged) {
      expect(filenameFor(testCase.node_id)).toBe(testCase.before_ts);
    }
  });

  it("is documented in HUP_SPEC.md, which is what a new SDK author reads", () => {
    const spec = readFileSync(SPEC_PATH, "utf8");
    expect(spec).toContain("[A-Za-z0-9._:-]");
    expect(spec.toLowerCase()).toContain("sha256");
    expect(spec).toContain("node_key_filename.json");
  });

  it("is asserted by the Python mirror against this same fixture", () => {
    const text = readFileSync(PY_MIRROR, "utf8");
    expect(text).toContain("node_key_filename.json");
  });
});
