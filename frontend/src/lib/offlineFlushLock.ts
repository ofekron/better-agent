import { uuidv4 } from "./uuid";

const FLUSH_LOCK = "better-agent-offline-flush";
const TAB_LOCK_PREFIX = "better-agent-tab:";

/** Identity of this tab, stamped on every dispatch claim it takes. */
export const OFFLINE_TAB_ID = uuidv4();

/**
 * Hold a lock named for this tab for as long as the page lives. The browser
 * releases it when the tab goes away, which is what lets other tabs tell a
 * genuinely in-flight dispatch from one orphaned by a closed or crashed tab —
 * without heartbeats or expiry guesses.
 */
export function holdTabPresence(): void {
  const locks = navigator.locks;
  if (!locks) return;
  void locks
    .request(`${TAB_LOCK_PREFIX}${OFFLINE_TAB_ID}`, () => new Promise<never>(() => {}))
    .catch(() => {});
}

/**
 * Ids of the tabs currently holding a presence lock, or `null` where the Web
 * Locks API is unavailable. `null` means "cannot tell" — callers must then
 * leave other tabs' claims alone rather than assume they are dead.
 */
export async function liveTabIds(): Promise<Set<string> | null> {
  const locks = navigator.locks;
  if (!locks?.query) return null;
  try {
    const snapshot = await locks.query();
    const live = new Set<string>();
    for (const held of snapshot.held ?? []) {
      if (held.name?.startsWith(TAB_LOCK_PREFIX)) {
        live.add(held.name.slice(TAB_LOCK_PREFIX.length));
      }
    }
    return live;
  } catch {
    return null;
  }
}

/**
 * Serialize the offline-backlog flush across every tab of this origin.
 *
 * The backlog lives in `localStorage`, which every tab shares, so each tab
 * runs its own flush over the same records. Resolves once this tab owns the
 * lock; call the returned function to release it.
 */
export async function acquireOfflineFlushLock(): Promise<() => void> {
  const locks = navigator.locks;
  if (!locks) return () => {};
  let release: () => void = () => {};
  await new Promise<void>((granted) => {
    locks
      .request(
        FLUSH_LOCK,
        () =>
          new Promise<void>((resolve) => {
            release = resolve;
            granted();
          }),
      )
      // Never leave the caller awaiting a lock it will not get — running the
      // flush unserialized is strictly better than not flushing at all.
      .catch(() => granted());
  });
  return release;
}
