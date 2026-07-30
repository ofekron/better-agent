export type BundleStatus =
  | "success"
  | "error"
  | "pending"
  | "downloading"
  | "deleted"
  | "deleting";

export interface BundleInfo {
  id: string;
  version: string;
  downloaded: string;
  checksum: string;
  status: BundleStatus;
}

interface CurrentBundleResult {
  bundle: BundleInfo;
  native: string;
}

interface UpdateFailedEvent {
  bundle: BundleInfo;
}

function nativeOnly(): never {
  throw new Error("CapacitorUpdater is unavailable on web");
}

export const CapacitorUpdater = {
  async notifyAppReady(): Promise<{ bundle: BundleInfo }> {
    return nativeOnly();
  },
  async current(): Promise<CurrentBundleResult> {
    return nativeOnly();
  },
  async getNextBundle(): Promise<BundleInfo | null> {
    return nativeOnly();
  },
  async getFailedUpdate(): Promise<UpdateFailedEvent | null> {
    return nativeOnly();
  },
  async list(): Promise<{ bundles: BundleInfo[] }> {
    return nativeOnly();
  },
  async download(_options: {
    url: string;
    version: string;
    checksum: string;
  }): Promise<BundleInfo> {
    return nativeOnly();
  },
  async next(_options: { id: string }): Promise<BundleInfo> {
    return nativeOnly();
  },
};
