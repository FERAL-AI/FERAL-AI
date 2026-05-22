import { describe, test, expect } from "vitest";
import { HUP_VERSION, buildFrame } from "../src/schemas";

describe("HUP protocol version", () => {
  test("HUP_VERSION is 1.3.0", () => {
    expect(HUP_VERSION).toBe("1.3.0");
  });

  test("node_register frame carries hup_version 1.3.0", () => {
    const frame = buildFrame("node_register", {
      node_id: "ts-test-node",
      node_type: "sensor",
      capabilities: ["heart_rate"],
    });
    expect(frame.hup_version).toBe("1.3.0");
    expect(frame.type).toBe("node_register");
  });

  test("node_bye frame carries hup_version 1.3.0", () => {
    const frame = buildFrame("node_bye", {
      reason: "shutdown",
      restart_in_s: 0,
    });
    expect(frame.hup_version).toBe("1.3.0");
    expect(frame.type).toBe("node_bye");
    expect(frame.payload.reason).toBe("shutdown");
  });

  test("node_heartbeat frame shape", () => {
    const frame = buildFrame("node_heartbeat", {
      ts: 1234567890.0,
    });
    expect(frame.type).toBe("node_heartbeat");
    expect(frame.hup_version).toBe("1.3.0");
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
