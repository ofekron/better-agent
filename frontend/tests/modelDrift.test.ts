import { describe, expect, it } from "vitest";
import {
  isLeakedProfileMirror,
  resolveModelDriftAction,
  filterStaleSelectorsPatch,
} from "../src/utils/modelDrift";
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
  it("patches when the user picks a new local model and the seq hasn't moved", () => {
    expect(
      resolveModelDriftAction({
        model: "claude-opus-5[1m]",
        sessionModel: "claude-sonnet-5",
        sessionSelectorsSeq: 3,
        lastAppliedSelectorsSeq: 3,
      }),
    ).toEqual({ kind: "patch", model: "claude-opus-5[1m]" });
  });

  it("adopts (never patches) when selectors_seq advanced since we last looked", () => {
    // Another tab/pane on the same session just PATCHed it — our local
    // `model` is stale, not a fresh user pick.
    expect(
      resolveModelDriftAction({
        model: "claude-sonnet-5",
        sessionModel: "claude-opus-5[1m]",
        sessionSelectorsSeq: 4,
        lastAppliedSelectorsSeq: 3,
      }),
    ).toEqual({ kind: "adopt", model: "claude-opus-5[1m]" });
  });

  it("does nothing when the seq advanced but local already agrees", () => {
    expect(
      resolveModelDriftAction({
        model: "claude-opus-5[1m]",
        sessionModel: "claude-opus-5[1m]",
        sessionSelectorsSeq: 4,
        lastAppliedSelectorsSeq: 3,
      }),
    ).toEqual({ kind: "none" });
  });

  it("does nothing once local and session model already agree (seq unchanged)", () => {
    expect(
      resolveModelDriftAction({
        model: "claude-sonnet-5",
        sessionModel: "claude-sonnet-5",
        sessionSelectorsSeq: 3,
        lastAppliedSelectorsSeq: 3,
      }),
    ).toEqual({ kind: "none" });
  });

  it("treats an unchanged seq as a genuine local edit, not a remote echo", () => {
    // seq staying at 3 across two runs means nothing new arrived from the
    // backend — a local/session mismatch here is a real, not-yet-persisted
    // user pick, so it must still patch (isStaleSeq is <=, not <: an
    // equal seq does NOT count as "advanced", so this correctly falls
    // through to the drift-check branch rather than being misread as a
    // remote update to adopt).
    expect(
      resolveModelDriftAction({
        model: "claude-sonnet-5",
        sessionModel: "claude-opus-5[1m]",
        sessionSelectorsSeq: 3,
        lastAppliedSelectorsSeq: 3,
      }),
    ).toEqual({ kind: "patch", model: "claude-sonnet-5" });
  });

  it("reproduces the bug: two tabs converge instead of ping-ponging when the seq keeps advancing", () => {
    // Regression for the real incident: two tabs open on the same session,
    // 105 model_switched events in ~30s, strictly alternating
    // claude-sonnet-5 <-> claude-opus-5[1m]. Model this as two independent
    // effect instances (tab A, tab B) reacting to each other's writes, with
    // the backend's `selectors_seq` strictly incrementing on every accepted
    // write (mirrors `SessionManager._bump_selectors_seq`).
    const SONNET = "claude-sonnet-5";
    const OPUS = "claude-opus-5[1m]";

    let sessionModel = SONNET;
    let sessionSeq = 0;
    let tabAModel = SONNET;
    let tabBModel = SONNET;
    let lastSeqA = 0;
    let lastSeqB = 0;

    // Tab A's user picks OPUS. Seq unchanged locally -> patch.
    tabAModel = OPUS;
    const actionA1 = resolveModelDriftAction({
      model: tabAModel,
      sessionModel,
      sessionSelectorsSeq: sessionSeq,
      lastAppliedSelectorsSeq: lastSeqA,
    });
    expect(actionA1).toEqual({ kind: "patch", model: OPUS });
    sessionModel = OPUS;
    sessionSeq += 1; // backend bump on the accepted write
    lastSeqA = sessionSeq;

    // Run both tabs' effects repeatedly, as a broadcast would trigger them,
    // with NO further user interaction on either side. A correct
    // implementation must settle (no more "patch" actions) within a couple
    // of rounds instead of oscillating indefinitely.
    let patchCount = 0;
    for (let round = 0; round < 20; round++) {
      const actionB = resolveModelDriftAction({
        model: tabBModel,
        sessionModel,
        sessionSelectorsSeq: sessionSeq,
        lastAppliedSelectorsSeq: lastSeqB,
      });
      lastSeqB = sessionSeq;
      if (actionB.kind === "adopt") tabBModel = actionB.model;
      if (actionB.kind === "patch") {
        patchCount++;
        sessionModel = actionB.model;
        sessionSeq += 1;
        lastSeqB = sessionSeq;
      }

      const actionA = resolveModelDriftAction({
        model: tabAModel,
        sessionModel,
        sessionSelectorsSeq: sessionSeq,
        lastAppliedSelectorsSeq: lastSeqA,
      });
      lastSeqA = sessionSeq;
      if (actionA.kind === "adopt") tabAModel = actionA.model;
      if (actionA.kind === "patch") {
        patchCount++;
        sessionModel = actionA.model;
        sessionSeq += 1;
        lastSeqA = sessionSeq;
      }
    }

    // No unbounded ping-pong: both tabs converge on OPUS and stop patching.
    expect(patchCount).toBe(0);
    expect(tabAModel).toBe(OPUS);
    expect(tabBModel).toBe(OPUS);
    expect(sessionModel).toBe(OPUS);
  });
});

describe("filterStaleSelectorsPatch", () => {
  it("drops a stale model/provider_id/reasoning_effort echo", () => {
    const patch = {
      model: "claude-sonnet-5",
      provider_id: "p-old",
      reasoning_effort: "low",
      selectors_seq: 3,
      cwd: "/keep/me",
    };
    const out = filterStaleSelectorsPatch(patch, 5);
    expect(out.model).toBeUndefined();
    expect(out.provider_id).toBeUndefined();
    expect(out.reasoning_effort).toBeUndefined();
    expect(out.selectors_seq).toBeUndefined();
    expect(out.cwd).toBe("/keep/me");
  });

  it("drops an equal-seq echo (already applied)", () => {
    const patch = { model: "claude-opus-5[1m]", selectors_seq: 5 };
    expect(filterStaleSelectorsPatch(patch, 5).model).toBeUndefined();
  });

  it("applies a newer selectors patch", () => {
    const patch = { model: "claude-opus-5[1m]", selectors_seq: 7 };
    const out = filterStaleSelectorsPatch(patch, 5);
    expect(out.model).toBe("claude-opus-5[1m]");
    expect(out.selectors_seq).toBe(7);
  });

  it("applies when seqs are undefined (legacy snapshot / patch without seq)", () => {
    expect(
      filterStaleSelectorsPatch({ model: "x", selectors_seq: 3 }, undefined).model,
    ).toBe("x");
    expect(
      filterStaleSelectorsPatch({ model: "x" }, 5).model,
    ).toBe("x");
  });

  it("passes patches with none of model/provider_id/reasoning_effort through untouched", () => {
    const patch = { cwd: "/x" };
    expect(filterStaleSelectorsPatch(patch, 5)).toBe(patch);
  });
});
