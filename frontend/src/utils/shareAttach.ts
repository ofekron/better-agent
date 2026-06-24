import type { PastedImage, Session } from "../types";

/** Build the draft patch for attaching OS-shared image(s) to a target
 *  session. MERGES the incoming images after the session's existing
 *  draft_images (never overwrites) and preserves the TARGET session's
 *  own draft_input — so attaching from the share sheet can't wipe an
 *  in-progress draft or queued attachments on that session. */
export function buildShareDraftPatch(
  target: Session | undefined,
  shared: PastedImage[]
): { draft_input: string; draft_images: PastedImage[] } {
  const existing = target?.draft_images ?? [];
  return {
    draft_input: target?.draft_input ?? "",
    draft_images: [...existing, ...shared],
  };
}
