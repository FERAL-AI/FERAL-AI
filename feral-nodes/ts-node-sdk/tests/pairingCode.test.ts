/**
 * Pairing codes must come from a CSPRNG.
 *
 * `generateCode` used `Math.floor(Math.random() * 1_000_000)` while
 * `crypto` was already imported at the top of the same file for the
 * node-id digest. Math.random is a fast non-cryptographic PRNG whose
 * internal state is recoverable from a small number of observed
 * outputs, so codes minted from it become predictable to anyone who has
 * seen a few. This code is the only thing between a stranger on the LAN
 * and a registered node.
 *
 * The Python SDK alongside this one always did it correctly, with
 * `secrets.randbelow` (python-node-sdk/src/feral_node_sdk/pairing.py:96),
 * so this was a slip in one language rather than a design decision.
 *
 * A statistical test cannot prove a source is cryptographic, so the
 * source check below is the one that actually pins the fix; the shape
 * and distribution tests guard the format around it.
 */

import { describe, expect, it } from "vitest";
import * as fs from "fs";
import * as path from "path";

import { generateCode } from "../src/pairing";

describe("generateCode", () => {
  it("does not use Math.random", () => {
    // The real assertion. Statistics cannot distinguish a CSPRNG from a
    // good non-cryptographic PRNG, so this reads the source.
    const src = fs.readFileSync(
      path.join(__dirname, "..", "src", "pairing.ts"),
      "utf8",
    );
    const code = src
      .split("\n")
      .filter((line) => !line.trim().startsWith("//") && !line.trim().startsWith("*"))
      .join("\n");
    expect(code).not.toContain("Math.random");
  });

  it("uses crypto.randomInt", () => {
    const src = fs.readFileSync(
      path.join(__dirname, "..", "src", "pairing.ts"),
      "utf8",
    );
    expect(src).toContain("crypto.randomInt");
  });

  it("is always six digits", () => {
    for (let i = 0; i < 200; i++) {
      expect(generateCode()).toMatch(/^\d{6}$/);
    }
  });

  it("keeps leading zeros rather than shortening the code", () => {
    // A code that sometimes renders as 5 characters would be silently
    // weaker and would break fixed-width entry fields.
    const codes = Array.from({ length: 500 }, () => generateCode());
    expect(codes.every((c) => c.length === 6)).toBe(true);
  });

  it("covers the full range including values below 100000", () => {
    // randomInt(1_000_000) must span [0, 1e6), not [100000, 1e6). If a
    // future change reached for a "make it always 6 digits" shortcut by
    // shifting the range, it would throw away ~10% of the keyspace.
    const codes = Array.from({ length: 3000 }, () => generateCode());
    expect(codes.some((c) => c.startsWith("0"))).toBe(true);
  });

  it("does not repeat over a large sample", () => {
    // Not a randomness proof. A stuck or seeded generator shows up here
    // immediately, which is the failure worth catching cheaply.
    const codes = new Set(Array.from({ length: 2000 }, () => generateCode()));
    expect(codes.size).toBeGreaterThan(1900);
  });
});
