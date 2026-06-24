import type { OrchestrationMode } from "../types";
import type { OrchestrationStrategy } from "./OrchestrationStrategy";
import { Strategy } from "./Strategy";

const strategies: Record<OrchestrationMode, OrchestrationStrategy> = {
  native: new Strategy("native"),
  team: new Strategy("team"),
  virtual: new Strategy("native"),
};

export function getStrategy(mode: OrchestrationMode | string | undefined): OrchestrationStrategy {
  return strategies[(mode as OrchestrationMode) ?? "native"] ?? strategies.native;
}
