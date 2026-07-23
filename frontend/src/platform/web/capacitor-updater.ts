export const CapacitorUpdater = {
  async notifyAppReady(): Promise<void> {},
  async current(): Promise<{ bundle?: { version?: string } }> {
    return {};
  },
  async download(options: {
    url: string;
    version: string;
    checksum: string;
  }): Promise<typeof options> {
    return options;
  },
  async set(_bundle: unknown): Promise<void> {},
};
