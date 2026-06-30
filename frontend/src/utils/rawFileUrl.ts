import { Capacitor } from "@capacitor/core";

import { withTokenQuery } from "../bearerAuth";

export function rawFileUrl(api: string, path: string, nodeId: string, version?: number): string {
  const params = new URLSearchParams({
    path,
    node_id: nodeId,
  });
  if (version !== undefined) {
    params.set("_v", String(version));
  }
  const base = `${api}/api/file/raw?${params.toString()}`;
  return Capacitor.isNativePlatform() ? withTokenQuery(base) : base;
}
