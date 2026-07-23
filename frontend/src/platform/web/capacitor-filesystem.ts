export const Filesystem = {
  async readFile(_options: { path: string }): Promise<{ data: string }> {
    throw new Error("Native filesystem is unavailable on web");
  },
};
