// Single source of truth for how a provider is labeled across the UI.
// `name` is the base label; an optional `nickname` is shown beside it to
// distinguish multiple accounts of the same kind (e.g. "Claude · Almog").

export function providerNickname(p?: { nickname?: string } | null): string {
  return (p?.nickname ?? "").trim();
}

export function providerDisplayName(
  p?: { name?: string; nickname?: string } | null,
  fallback = "",
): string {
  const name = (p?.name ?? "").trim();
  const nickname = providerNickname(p);
  const base = name || fallback;
  return nickname ? `${base} · ${nickname}` : base;
}
