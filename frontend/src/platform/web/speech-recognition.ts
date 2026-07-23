export const SpeechRecognition = {
  async available(): Promise<{ available: false }> {
    return { available: false };
  },
  async checkPermissions(): Promise<{
    speechRecognition: "prompt" | "granted" | "denied";
  }> {
    return { speechRecognition: "denied" };
  },
  async requestPermissions(): Promise<{
    speechRecognition: "prompt" | "granted" | "denied";
  }> {
    return { speechRecognition: "denied" };
  },
  async start(_options?: {
    language?: string;
    maxResults?: number;
    partialResults?: boolean;
    popup?: boolean;
  }): Promise<{ matches: string[] }> {
    return { matches: [] };
  },
  async stop(): Promise<void> {},
  async removeAllListeners(): Promise<void> {},
};
