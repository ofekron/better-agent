import { describe, expect, it } from "vitest";
import { isLeakedProfileMirror, resolveModelDriftAction } from "../src/utils/modelDrift";
import { makeRuntimeProfile } from "./fixtures";

const zaiProfile = makeRuntimeProfile({
  id: "rp-zai",
  provider_id: "zai",
  name: "Z.AI",
  default_model: "glm-5.2",
});
const lastModels = { "rp-zai": "glm-5.1" };

describe("isLeakedProfileMirror", () => {
  it("suppresses the Z.AI default leaking onto a Claude session", () => {
    // The exact bug: default profile switched to Z.AI, its default_model
    // glm-5.2 sits in the global `model` mirror, session's provider is Claude.
    expect(isLeakedProfileMirror("glm-5.2", "claude", zaiProfile, lastModels)).toBe(true);
  });

  it("suppresses the default profile's last-used model too", () => {
    expect(isLeakedProfileMirror("glm-5.1", "claude", zaiProfile, lastModels)).toBe(true);
  });

  it("does NOT suppress a legit model change within the same provider", () => {
    // Session provider === default profile's provider → a real user
    // selection, persist it.
    expect(isLeakedProfileMirror("glm-5.2", "zai", zaiProfile, lastModels)).toBe(false);
  });

  it("does NOT suppress a session model unrelated to the default mirror", () => {
    expect(isLeakedProfileMirror("opus", "claude", zaiProfile, lastModels)).toBe(false);
  });

  it("is inert when profile, provider, or model are missing", () => {
    expect(isLeakedProfileMirror("", "claude", zaiProfile, lastModels)).toBe(false);
    expect(isLeakedProfileMirror("glm-5.2", undefined, zaiProfile, lastModels)).toBe(false);
    expect(isLeakedProfileMirror("glm-5.2", "claude", null, lastModels)).toBe(false);
  });
});

describe("resolveModelDriftAction", () => {
  it("patches when the user picks a new local model (session unchanged)", () => {
    expect(
      resolveModelDriftAction({
        model: "claude-opus-5[1m]",
        sessionModel: "claude-sonnet-5",
        prevSessionModel: "claude-sonnet-5",
      }),
    ).toEqual({ kind: "patch", model: "claude-opus-5[1m]" });
  });

  it("adopts (never patches) when the session's model moved since last render", () => {
    // Another tab/pane on the same session just PATCHed it — our local
    // `model` is now stale, not a fresh user pick.
    expect(
      resolveModelDriftAction({
        model: "claude-sonnet-5",
        sessionModel: "claude-opus-5[1m]",
        prevSessionModel: "claude-sonnet-5",
      }),
    ).toEqual({ kind: "adopt", model: "claude-opus-5[1m]" });
  });

  it("does nothing on the first render (no prior session model to compare against)", () => {
    expect(
      resolveModelDriftAction({
        model: "claude-sonnet-5",
        sessionModel: "claude-sonnet-5",
        prevSessionModel: null,
      }),
    ).toEqual({ kind: "none" });
  });

  it("does nothing once local and session model already agree", () => {
    expect(
      resolveModelDriftAction({
        model: "claude-sonnet-5",
        sessionModel: "claude-sonnet-5",
        prevSessionModel: "claude-opus-5[1m]",
      }),
    ).toEqual({ kind: "none" });
  });

  it("reproduces the bug: two tabs converge instead of ping-ponging forever", () => {
    // Regression for the real incident: two tabs open on the same session,
    // 105 model_switched events in ~30s, strictly alternating
    // claude-sonnet-5 <-> claude-opus-5[1m], driven by each tab "correcting"
    // the other tab's broadcast. Model this as two independent effect
    // instances (tab A, tab B) reacting to each other's writes.
    const SONNET = "claude-sonnet-5";
    const OPUS = "claude-opus-5[1m]";

    let sessionModel = SONNET; // shared durable state, as if on the backend
    let tabAModel = SONNET;
    let tabBModel = SONNET;
    let prevSessionModelA: string | null = SONNET;
    let prevSessionModelB: string | null = SONNET;

    // Tab A's user picks OPUS. Its own effect run patches the session.
    tabAModel = OPUS;
    const actionA1 = resolveModelDriftAction({
      model: tabAModel,
      sessionModel,
      prevSessionModel: prevSessionModelA,
    });
    expect(actionA1).toEqual({ kind: "patch", model: OPUS });
    sessionModel = OPUS; // simulates the successful PATCH landing
    prevSessionModelA = sessionModel;

    // Run both tabs' effects repeatedly, as a broadcast would trigger them,
    // with NO further user interaction on either side. A correct
    // implementation must settle (no more "patch" actions) within a couple
    // of rounds instead of oscillating indefinitely.
    let patchCount = 0;
    for (let round = 0; round < 20; round++) {
      const actionB = resolveModelDriftAction({
        model: tabBModel,
        sessionModel,
        prevSessionModel: prevSessionModelB,
      });
      prevSessionModelB = sessionModel;
      if (actionB.kind === "adopt") tabBModel = actionB.model;
      if (actionB.kind === "patch") {
        patchCount++;
        sessionModel = actionB.model;
      }

      const actionA = resolveModelDriftAction({
        model: tabAModel,
        sessionModel,
        prevSessionModel: prevSessionModelA,
      });
      prevSessionModelA = sessionModel;
      if (actionA.kind === "adopt") tabAModel = actionA.model;
      if (actionA.kind === "patch") {
        patchCount++;
        sessionModel = actionA.model;
      }
    }

    // No unbounded ping-pong: both tabs converge on OPUS and stop patching.
    expect(patchCount).toBe(0);
    expect(tabAModel).toBe(OPUS);
    expect(tabBModel).toBe(OPUS);
    expect(sessionModel).toBe(OPUS);
  });
});
