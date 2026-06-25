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

/** Additive union of incoming draft_images into the composer's current
 *  images. Appends only entries not already present (by base64), so an
 *  externally-injected image (OS share sheet attaching to the already-
 *  open session) surfaces without clobbering in-progress local
 *  composition. Never removes local-only entries — the send-clear path
 *  owns removal. */
export function mergeIncomingImages(
  current: PastedImage[],
  incoming: PastedImage[]
): PastedImage[] {
  const have = new Set(current.map((i) => i.base64));
  const fresh = incoming.filter((i) => !have.has(i.base64));
  return fresh.length === 0 ? current : [...current, ...fresh];
}
