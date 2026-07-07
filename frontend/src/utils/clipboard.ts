/** Copy text to clipboard with a textarea + execCommand fallback for
 *  insecure contexts (HTTP / non-localhost) and older mobile WebViews
 *  where the async Clipboard API is unavailable or rejects silently.
 *  The textarea is kept in the viewport (top-left, 1×1px, transparent)
 *  and explicitly focused so that iOS Safari and Android WebView accept
 *  execCommand("copy"). Without this fallback, copy actions invoked from
 *  mobile action sheets do nothing. */
export async function copyToClipboard(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch {
    // Clipboard API unavailable (insecure context) or denied — fall through.
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.top = "0";
  ta.style.left = "0";
  ta.style.width = "1px";
  ta.style.height = "1px";
  ta.style.padding = "0";
  ta.style.border = "none";
  ta.style.outline = "none";
  ta.style.boxShadow = "none";
  ta.style.background = "transparent";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try {
    document.execCommand("copy");
  } catch {
    // best effort
  }
  document.body.removeChild(ta);
}
