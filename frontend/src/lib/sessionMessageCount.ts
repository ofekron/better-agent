import type { Session } from "src/types";

export function sessionMessageCount(session: Pick<Session, "message_count" | "messages">): number {
  return session.message_count ?? session.messages?.filter((message) => message.role === "user").length ?? 0;
}
