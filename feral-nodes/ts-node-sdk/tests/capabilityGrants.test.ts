import { describe, test, expect, beforeEach } from "vitest";
import { FeralNode } from "../src/node";

/**
 * HUP_SPEC.md section 6, node side.
 *
 * Two defects, one test file.
 *
 * 1. **The grant was never read.** `this.granted` was assigned from
 *    `node_ack` and then nothing in the SDK ever consulted it. A handler
 *    registered for a capability the operator had denied ran exactly as
 *    if it had not been. The spec's "nodes MUST refuse any
 *    hup_action_request whose name is not in their registered
 *    capabilities" was satisfied only by the handler-lookup miss, which
 *    is a different question.
 *
 * 2. **The fallback failed open.** The assignment was
 *
 *        granted_capabilities.length ? granted_capabilities : capabilities
 *
 *    so an empty array -- a brain saying "you may do nothing" -- was
 *    falsy and got replaced with everything this node declared. The one
 *    answer the operator most needs to be able to give was the one that
 *    could not be transmitted.
 *
 * The private fields are reached through a cast rather than driven over a
 * socket: the frame handler and the dispatcher are the units under test,
 * and standing up a WebSocket to reach them would be testing `ws`.
 */

type NodeInternals = {
  handleFrame(raw: string): Promise<void>;
  dispatchAction(req: Record<string, unknown>): Promise<void>;
  sendActionResponse(
    actionId: string,
    extras: Record<string, unknown>,
  ): Promise<void>;
  granted: Set<string> | null;
  capabilities: string[];
};

function makeNode() {
  const node = new FeralNode({
    nodeId: "ts-grant-node",
    nodeType: "sensor",
    capabilities: ["camera", "heart_rate", "buzzer"],
    brainUrl: "ws://127.0.0.1:1/v1/node",
    apiKey: "test",
  });
  const responses: Array<Record<string, unknown>> = [];
  const internals = node as unknown as NodeInternals;
  internals.sendActionResponse = async (actionId, extras) => {
    responses.push({ action_id: actionId, ...extras });
  };
  return { node, internals, responses };
}

function ack(payload: Record<string, unknown>): string {
  return JSON.stringify({ type: "node_ack", payload });
}

describe("node_ack grant handling", () => {
  let ctx: ReturnType<typeof makeNode>;
  beforeEach(() => { ctx = makeNode(); });

  test("an explicit grant list is taken verbatim", async () => {
    await ctx.internals.handleFrame(
      ack({ granted_capabilities: ["heart_rate"], denied_capabilities: ["camera"] }),
    );
    expect([...(ctx.internals.granted ?? [])]).toEqual(["heart_rate"]);
  });

  test("an EMPTY grant list is a full deny, not a fallback", async () => {
    await ctx.internals.handleFrame(ack({ granted_capabilities: [] }));
    expect(ctx.internals.granted).not.toBeNull();
    expect(ctx.internals.granted?.size).toBe(0);
  });

  test("an omitted key falls back to this node's own declaration", async () => {
    // A brain with no grant store. Only this case may be read as
    // "everything I declared".
    await ctx.internals.handleFrame(ack({ heartbeat_ms: 5000 }));
    expect([...(ctx.internals.granted ?? [])].sort())
      .toEqual(["buzzer", "camera", "heart_rate"]);
  });

  test("granted is null until an ack lands", () => {
    // Not the empty set. The dispatcher has to tell "nobody told me yet"
    // apart from "you may do nothing", and the old `Set()` initial value
    // made those the same value.
    expect(ctx.internals.granted).toBeNull();
  });
});

describe("action dispatch honours the grant", () => {
  let ctx: ReturnType<typeof makeNode>;
  beforeEach(() => { ctx = makeNode(); });

  test("refuses an action outside granted_capabilities", async () => {
    ctx.node.onAction("camera", async () => ({ ok: true }));
    await ctx.internals.handleFrame(ack({ granted_capabilities: ["heart_rate"] }));
    await ctx.internals.dispatchAction({ action_id: "a1", name: "camera", params: {} });
    expect(ctx.responses).toHaveLength(1);
    expect(ctx.responses[0].success).toBe(false);
    expect(String(ctx.responses[0].error)).toContain("capability_denied");
  });

  test("the registered handler does NOT run for a denied capability", async () => {
    let ran = false;
    ctx.node.onAction("camera", async () => { ran = true; return { ok: true }; });
    await ctx.internals.handleFrame(ack({ granted_capabilities: [] }));
    await ctx.internals.dispatchAction({ action_id: "a1", name: "camera", params: {} });
    expect(ran).toBe(false);
  });

  test("a granted action still runs", async () => {
    let ran = false;
    ctx.node.onAction("buzzer", async () => { ran = true; return { ok: true }; });
    await ctx.internals.handleFrame(ack({ granted_capabilities: ["buzzer"] }));
    await ctx.internals.dispatchAction({ action_id: "a1", name: "buzzer", params: {} });
    expect(ran).toBe(true);
    expect(ctx.responses[0].success).toBe(true);
  });

  test("before any ack, dispatch is not blocked", async () => {
    // Fail-open on "nobody has told me yet" is deliberate: a node that
    // reconnects and gets an action before its ack must not refuse it.
    let ran = false;
    ctx.node.onAction("buzzer", async () => { ran = true; return { ok: true }; });
    await ctx.internals.dispatchAction({ action_id: "a1", name: "buzzer", params: {} });
    expect(ran).toBe(true);
  });
});
