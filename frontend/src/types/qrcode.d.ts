// Ambient declaration for the `qrcode` package, which ships no types.
// Minimal surface — only what MobileSetup.tsx uses.
declare module "qrcode" {
  interface QRCodeToDataURLOptions {
    width?: number;
    margin?: number;
    color?: { dark?: string; light?: string };
  }
  export function toDataURL(
    text: string,
    options?: QRCodeToDataURLOptions
  ): Promise<string>;
}
