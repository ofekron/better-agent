/** Canonical RFC-4122 v4 UUID generator that works in non-secure contexts.
 *
 * `crypto.randomUUID()` is only exposed in secure contexts (https or
 * localhost). When the app is served over a plain-http LAN address (e.g.
 * a Tailscale/machine IP), it is `undefined`, so calling it throws and any
 * client-generated id (session ids, client_session_id) is lost. This falls
 * back to `crypto.getRandomValues`, which IS available in insecure contexts,
 * to build a canonical lowercase v4 UUID the backend's validation accepts.
 */
export function uuidv4(): string {
  const native = globalThis.crypto?.randomUUID;
  if (typeof native === "function") return native.call(globalThis.crypto);

  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  // Version 4 and RFC-4122 variant bits.
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex: string[] = [];
  for (let i = 0; i < 256; i++) hex.push((i + 0x100).toString(16).slice(1));
  return (
    hex[bytes[0]] + hex[bytes[1]] + hex[bytes[2]] + hex[bytes[3]] +
    "-" + hex[bytes[4]] + hex[bytes[5]] +
    "-" + hex[bytes[6]] + hex[bytes[7]] +
    "-" + hex[bytes[8]] + hex[bytes[9]] +
    "-" + hex[bytes[10]] + hex[bytes[11]] + hex[bytes[12]] + hex[bytes[13]] + hex[bytes[14]] + hex[bytes[15]]
  );
}
