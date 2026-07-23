export const Browser = {
  async open({ url }: { url: string }): Promise<void> {
    window.open(url, "_blank", "noopener,noreferrer");
  },
};
