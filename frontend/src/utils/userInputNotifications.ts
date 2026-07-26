import type { UserInteractionRequest } from "../types";
import "../desktopBridge";

function requestBody(request: UserInteractionRequest): string {
  if (request.kind === "approval") return request.prompt;
  if (request.kind === "memory") return request.memory_proposal.description;
  return request.questions[0]?.question ?? "";
}

export async function notifyUserRequest(
  request: UserInteractionRequest,
  approvalTitle: string,
  inputTitle: string,
  memoryTitles: { add: string; edit: string },
): Promise<void> {
  const title =
    request.kind === "approval"
      ? approvalTitle
      : request.kind === "memory"
        ? request.memory_proposal.action === "add"
          ? memoryTitles.add
          : memoryTitles.edit
        : inputTitle;
  const body = requestBody(request);
  try {
    const desktopNotify = window.pywebview?.api?.notify_user;
    if (desktopNotify) {
      await desktopNotify(title, body);
      return;
    }
    if (typeof Notification !== "undefined" && Notification.permission === "granted") {
      new Notification(title, { body, tag: `better-agent:${request.request_id}` });
    }
  } catch {
    // Notification failure must never block the in-app request card.
  }
}
