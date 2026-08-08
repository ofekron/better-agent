// Shared "native intent (already submitted), REST fallback otherwise" gate
// for a void mutation that THROWS on a real v2 rejection — the exact idiom
// `components/SettingsPage.tsx`'s `submitProviderMutation` and
// `lib/scheduleMutations.ts`'s `deleteScheduleNativeOrRest` each derived
// independently: `submitted` is already `null` when there's no v2
// equivalent for this call OR the shared socket wasn't open (both collapse
// to `null` at the caller's own `submitXIntent(...)` call, same not-open
// contract every v2 write path in this app uses) -> run `restFallback`
// instead; a v2 `intent_rejected` throws (landing in the SAME catch path a
// REST failure already would); a v2 accept resolves with no further REST
// call.
//
// NOT the right shape for every native-when-open caller in this app:
// `lib/interactionResolveSocket.ts`'s `tryResolveInteraction` and
// `hooks/useSession.ts`'s `trySessionIntentV2` both return a boolean
// instead of throwing, and — critically — disagree with EACH OTHER on what
// a rejection means (`tryResolveInteraction` treats a rejection as
// "handled, no REST retry — both paths would fail identically"; a chat
// approval/response has no meaningful REST fallback once a real business
// rejection comes back, so retrying over REST would just fail the same way
// twice; `trySessionIntentV2` treats a rejection as "fall back to REST",
// since session mutations DO have a REST endpoint that might still
// succeed, e.g. under different validation). Neither can converge onto
// this helper, or onto each other, without changing real behavior — kept
// as their own separate implementations.
import type { TransportAckFrame } from "../adapter/wire";

export async function nativeOrRestMutation(
  submitted: Promise<TransportAckFrame> | null,
  restFallback: () => Promise<void>,
): Promise<void> {
  if (!submitted) {
    await restFallback();
    return;
  }
  const ack = await submitted;
  if (ack.type === "intent_rejected") {
    throw new Error(ack.message || `rejected (${ack.code})`);
  }
}
