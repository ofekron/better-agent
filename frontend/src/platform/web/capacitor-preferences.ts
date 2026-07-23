export const Preferences = {
  async get({ key }: { key: string }): Promise<{ value: string | null }> {
    return { value: localStorage.getItem(key) };
  },
  async set({ key, value }: { key: string; value: string }): Promise<void> {
    localStorage.setItem(key, value);
  },
};
