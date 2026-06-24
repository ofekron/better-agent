import { API } from "../api";

export function buildMessageImageUrl(sessionId: string | undefined, filename: string | undefined): string {
  if (!sessionId || !filename) return "";
  return `${API}/api/sessions/${encodeURIComponent(sessionId)}/images/${encodeURIComponent(filename)}`;
}
