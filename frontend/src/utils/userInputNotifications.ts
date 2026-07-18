import type { UserInteractionRequest } from "../types";

declare global {
  interface Window {
    pywebview?: {
      api?: {
        notify_user?: (title: string, body: string) => Promise<unknown>;
      };
    };
  }
}

function requestBody(request: UserInteractionRequest): string {
  if (request.kind === "approval") return request.prompt;
  return request.questions[0]?.question ?? "";
}

export async function notifyUserRequest(
  request: UserInteractionRequest,
  approvalTitle: string,
  inputTitle: string,
): Promise<void> {
  const title = request.kind === "approval" ? approvalTitle : inputTitle;
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
