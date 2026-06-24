import type { OrchestrationMode } from "../types";

export function normalizeOrchestrationMode(value: unknown): OrchestrationMode {
  if (value === "manager") return "team";
  if (value === "team" || value === "native" || value === "virtual") return value;
  return "native";
}
