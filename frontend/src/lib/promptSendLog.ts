type PromptSendLogLevel = "info" | "warn" | "error";

export function logPromptSend(
  stage: string,
  data: Record<string, unknown>,
  level: PromptSendLogLevel = "info",
): void {
  console[level]("[prompt-send]", { stage, ...data });
}
