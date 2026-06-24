const HARD_REFRESH_PARAM = "_better_agent_refresh";

interface RefreshNavigation {
  href: string;
  replace(url: string): void;
}

interface RefreshRegistration {
  unregister(): Promise<boolean>;
}

export async function hardRefreshCurrentPage(
  requestId: string,
  navigation: RefreshNavigation = window.location,
  getRegistration: () => Promise<RefreshRegistration | undefined> | undefined =
    () => navigator.serviceWorker?.getRegistration(),
  clearCaches: () => Promise<unknown> = async () => {
    const keys = await caches.keys();
    await Promise.all(keys.map((key) => caches.delete(key)));
  },
) {
  try {
    const registration = await getRegistration();
    await registration?.unregister();
  } catch {
    // Continue with the remaining cache invalidation steps.
  }
  try {
    await clearCaches();
  } catch {
    // Cache-busted navigation still forces a fresh document request.
  }

  const url = new URL(navigation.href);
  url.searchParams.set(HARD_REFRESH_PARAM, requestId);
  navigation.replace(url.href);
}

export function clearHardRefreshMarker(
  href = window.location.href,
  replaceState: (url: string) => void =
    (url) => window.history.replaceState(window.history.state, "", url),
) {
  const url = new URL(href);
  if (!url.searchParams.has(HARD_REFRESH_PARAM)) return;

  url.searchParams.delete(HARD_REFRESH_PARAM);
  replaceState(`${url.pathname}${url.search}${url.hash}`);
}
