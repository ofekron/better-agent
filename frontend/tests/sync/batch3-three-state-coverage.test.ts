import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const useSessionSource = readFileSync(
  `${process.cwd()}/src/hooks/useSession.ts`,
  "utf8",
);
const backlogSource = readFileSync(
  `${process.cwd()}/src/utils/writeBacklog.ts`,
  "utf8",
);
const appSource = readFileSync(`${process.cwd()}/src/App.tsx`, "utf8");
const chatSource = readFileSync(`${process.cwd()}/src/components/Chat.tsx`, "utf8");

describe("batch 3 three-state coverage", () => {
  it.each([
    "session:fork:",
    "session:delete:",
    "session:pin:",
    "session:rename:",
  ])("routes %s through the canonical controller", (operationId) => {
    const operation = useSessionSource.indexOf(operationId);
    expect(operation).toBeGreaterThan(-1);
    expect(useSessionSource.slice(Math.max(0, operation - 240), operation + 2200)).toContain(
      "runThreeStateSync",
    );
  });

  it("keeps durable backlog removal gated by an explicit successful response", () => {
    expect(backlogSource).toContain("if (res.ok) succeeded.add(w)");
    expect(backlogSource).not.toContain("expectedAuthoritativeState");
    expect(backlogSource).not.toContain("runThreeStateSync");
  });

  it.each([
    "session:worker-policy:",
    "selectors:save:",
    "rateLimitContinue:",
    "session:supervisorToggle:",
    "session:separateSupervisor:",
    "session:supervisorReview:",
  ])("routes App operation %s through the canonical controller", (operationId) => {
    const operation = appSource.indexOf(operationId);
    expect(operation).toBeGreaterThan(-1);
    expect(appSource.slice(Math.max(0, operation - 260), operation + 2200)).toContain(
      "runThreeStateSync",
    );
  });

  it.each(["approval:tool:", "approval:worker:", "approval:credential:", "session:rewind:"])(
    "routes Chat operation %s through the canonical controller",
    (operationId) => {
      const operation = chatSource.indexOf(operationId);
      expect(operation).toBeGreaterThan(-1);
      expect(chatSource.slice(Math.max(0, operation - 260), operation + 1800)).toContain(
        "runThreeStateSync",
      );
    },
  );
});
