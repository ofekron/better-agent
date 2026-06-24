import type { FileDiscussion, Session } from "../types";

export function upsertFileDiscussionMeta(
  meta: Session["working_mode_meta"],
  discussion: FileDiscussion,
): NonNullable<Session["working_mode_meta"]> {
  const next = { ...(meta ?? {}) };
  const discussions = [...(next.file_discussions ?? [])];
  const idx = discussions.findIndex((current) => current.id === discussion.id);
  if (idx >= 0) discussions[idx] = discussion;
  else discussions.push(discussion);
  next.file_discussions = discussions;
  return next;
}

export function patchFileDiscussionMeta(
  meta: Session["working_mode_meta"],
  discussionId: string,
  discussion: FileDiscussion,
): NonNullable<Session["working_mode_meta"]> {
  const next = { ...(meta ?? {}) };
  next.file_discussions = (next.file_discussions ?? []).map((current) =>
    current.id === discussionId ? discussion : current,
  );
  return next;
}
