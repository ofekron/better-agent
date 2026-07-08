import { withTokenQuery } from "../bearerAuth";

export function rawFileUrl(api: string, path: string, nodeId: string, version?: number): string {
  const params = new URLSearchParams({
    path,
    node_id: nodeId,
  });
  if (version !== undefined) {
    params.set("_v", String(version));
  }
  // ?token= rides along whenever a bearer token is stored (no-op
  // otherwise) — raw <img>/<a> loads can't send the Authorization
  // header and the session cookie can't travel in cross-site embeds.
  return withTokenQuery(`${api}/api/file/raw?${params.toString()}`);
}
