import { describe, test, expect } from "vitest";
import { HUP_VERSION, buildFrame } from "../src/schemas";

// The version literal is asserted ONCE, against the constant, and every
// frame test then compares against HUP_VERSION rather than repeating the
// string. Restating "1.3.0" in five places is why this file failed as
// five separate assertions on a single-line bump: one fact, one place to
// change it, and the frame tests keep asserting what they actually care
// about, which is that buildFrame stamps the SDK's version onto every
// envelope.
const EXPECTED_HUP_VERSION = "1.4.0";

describe("HUP protocol version", () => {
  test(`HUP_VERSION is ${EXPECTED_HUP_VERSION}`, () => {
    expect(HUP_VERSION).toBe(EXPECTED_HUP_VERSION);
  });

  test("node_register frame carries the SDK's hup_version", () => {
    const frame = buildFrame("node_register", {
      node_id: "ts-test-node",
      node_type: "sensor",
      capabilities: ["heart_rate"],
    });
    expect(frame.hup_version).toBe(HUP_VERSION);
    expect(frame.type).toBe("node_register");
  });

  test("node_bye frame carries the SDK's hup_version", () => {
    const frame = buildFrame("node_bye", {
      reason: "shutdown",
      restart_in_s: 0,
    });
    expect(frame.hup_version).toBe(HUP_VERSION);
    expect(frame.type).toBe("node_bye");
    expect(frame.payload.reason).toBe("shutdown");
  });

  test("node_heartbeat frame shape", () => {
    const frame = buildFrame("node_heartbeat", {
      ts: 1234567890.0,
    });
    expect(frame.type).toBe("node_heartbeat");
    expect(frame.hup_version).toBe(HUP_VERSION);
  });

  test("hup_action_response frame shape", () => {
    const frame = buildFrame("hup_action_response", {
      action_id: "act-001",
      success: true,
      result: { vibrated_ms: 250 },
    });
    expect(frame.type).toBe("hup_action_response");
    expect(frame.payload.action_id).toBe("act-001");
    expect(frame.payload.success).toBe(true);
  });
});
