/** Copy text to clipboard with a textarea + execCommand fallback for
 *  insecure contexts (HTTP / non-localhost) and older mobile WebViews
 *  where the async Clipboard API is unavailable or rejects silently.
 *  The textarea is kept in the viewport (top-left, 1×1px, transparent)
 *  and explicitly focused so that iOS Safari and Android WebView accept
 *  execCommand("copy"). Without this fallback, copy actions invoked from
 *  mobile action sheets do nothing. */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Clipboard API unavailable (insecure context) or denied — fall through.
  }
  const activeElement =
    document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const selection = window.getSelection();
  const selectedRanges = selection
    ? Array.from({ length: selection.rangeCount }, (_, index) =>
        selection.getRangeAt(index).cloneRange(),
      )
    : [];
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
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    document.body.removeChild(ta);
    if (selection && selectedRanges.length > 0) {
      selection.removeAllRanges();
      selectedRanges.forEach((range) => selection.addRange(range));
    }
    activeElement?.focus({ preventScroll: true });
  }
}
