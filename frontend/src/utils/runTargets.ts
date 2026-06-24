import type { RunInfo } from "../types";

export function isUnanchoredRun(run: Pick<RunInfo, "target_message_id">): boolean {
  return run.target_message_id == null;
}
