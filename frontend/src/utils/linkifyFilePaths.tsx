import {
  Children,
  cloneElement,
  createElement,
  Fragment,
  isValidElement,
  type ReactElement,
  type ReactNode,
  type SyntheticEvent,
} from "react";
import type { FileFocus } from "../types";
import { openExternalLink } from "./externalLink";
import {
  MediaPreviewInline,
  getMediaType,
} from "../components/MediaPreviewInline";

const WIN_ABS_RE = /^[A-Za-z]:[/\\]/;
const URL_SCHEME_RE = /^[A-Za-z][A-Za-z0-9+.-]*:/;
const FILE_LIKE_RE = /(?:^|[/\\])[^/\\?#]+\.[A-Za-z0-9]{1,8}(?::\d+(?:-\d+)?)?$/;
const REL_FILE_WITH_LINE_RE = /^[^:/\\?#]+\.[A-Za-z0-9]{1,8}:\d+(?:-\d+)?$/;
const TRAILING_SLASH_RE = /[/\\]+$/;
const SESSION_LINK_MARKER_RE = /\[\[ba-session:([^|\]\n]+)\|([^\]\n]*)\]\]/g;

/** True for POSIX (`/x`), Windows drive (`C:\x`, `C:/x`) and UNC
 * (`\\server`) absolute paths. Mirrors the backend file_ref_resolver
 * `_is_absolute` so a Windows abs path isn't mistakenly joined to cwd. */
export function isAbsolutePath(path: string): boolean {
  return path.startsWith("/") || path.startsWith("\\") || WIN_ABS_RE.test(path);
}

/** Parse a `bcfile:` href produced by the backend's `file_ref_resolver`.
 * Format: `bcfile:<urlencoded-abs-path>?L=<start>[-<end>]`. Returns
 * `null` if the input isn't a bcfile href. */
function parseBcfileHref(href: string): { path: string; focus?: FileFocus } | null {
  if (!href || !href.startsWith("bcfile:")) return null;
  const body = href.slice("bcfile:".length);
  const qIdx = body.indexOf("?");
  const rawPath = qIdx === -1 ? body : body.slice(0, qIdx);
  const query = qIdx === -1 ? "" : body.slice(qIdx + 1);
  let path: string;
  try {
    path = decodeURIComponent(rawPath);
  } catch {
    path = rawPath;
  }
  if (!path) return null;
  let focus: FileFocus | undefined;
  if (query) {
    const params = new URLSearchParams(query);
    const lines = params.get("L");
    if (lines) {
      const m = /^(\d+)(?:-(\d+))?$/.exec(lines);
      if (m) {
        const startLine = parseInt(m[1], 10);
        const endLine = m[2] ? parseInt(m[2], 10) : startLine;
        focus = { startLine, endLine };
      }
    }
  }
  return { path, focus };
}

export function parseMarkdownFileHref(href: string): { path: string; focus?: FileFocus } | null {
  if (!href || href.startsWith("#")) return null;
  if (href.startsWith("bcfile:")) return parseBcfileHref(href);
  if (URL_SCHEME_RE.test(href) && !WIN_ABS_RE.test(href) && !REL_FILE_WITH_LINE_RE.test(href)) {
    return null;
  }

  const queryIndex = href.search(/[?#]/);
  const rawPath = queryIndex === -1 ? href : href.slice(0, queryIndex);
  if (!FILE_LIKE_RE.test(rawPath)) return null;

  let pathWithLine: string;
  try {
    pathWithLine = decodeURIComponent(rawPath);
  } catch {
    pathWithLine = rawPath;
  }

  const lineMatch = /:(\d+)(?:-(\d+))?$/.exec(pathWithLine);
  if (!lineMatch) return { path: pathWithLine };

  const path = pathWithLine.slice(0, lineMatch.index);
  const startLine = parseInt(lineMatch[1], 10);
  const endLine = lineMatch[2] ? parseInt(lineMatch[2], 10) : startLine;
  return { path, focus: { startLine, endLine } };
}

function focusSuffix(focus?: FileFocus): string {
  if (!focus) return "";
  if (focus.startLine === focus.endLine) return `:${focus.startLine}`;
  return `:${focus.startLine}-${focus.endLine}`;
}

function fileLinkTitle(path: string, focus?: FileFocus): string {
  return `${path}${focusSuffix(focus)}`;
}

function pathBasename(path: string): string {
  const clean = path.replace(TRAILING_SLASH_RE, "");
  const parts = clean.split(/[/\\]/);
  return parts.at(-1) || clean || path;
}

function plainText(children: ReactNode): string | null {
  if (typeof children === "string") return children;
  if (typeof children === "number") return String(children);
  if (Array.isArray(children)) {
    let text = "";
    for (const child of children) {
      const childText = plainText(child);
      if (childText === null) return null;
      text += childText;
    }
    return text;
  }
  return null;
}

function normalizeComparableLink(value: string): string {
  return value.trim().replace(TRAILING_SLASH_RE, "");
}

function decodeMarkerPart(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function sessionLinkLabel(sessionId: string, name: string): string {
  const label = name.trim() || "Session";
  return `${label} · ${sessionId.slice(0, 4)}`;
}

function sessionPath(sessionId: string): string {
  return `/s/${encodeURIComponent(sessionId)}`;
}

function parseSessionHref(href: string): { sessionId: string } | null {
  const m = href.match(/^\/s\/([^/?#]+)\/?$/);
  if (!m) return null;
  try {
    return { sessionId: decodeURIComponent(m[1]) };
  } catch {
    return { sessionId: m[1] };
  }
}

function openSession(sessionId: string) {
  const path = sessionPath(sessionId);
  if (window.location.pathname !== path) window.history.pushState(null, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function sessionLinkMarker(sessionId: string, name: string): string {
  return `[[ba-session:${encodeURIComponent(sessionId)}|${encodeURIComponent(name)}]]`;
}

export function sessionMarkersToMarkdown(text: string): string {
  return text.replace(SESSION_LINK_MARKER_RE, (_whole, rawId, rawName) => {
    const sessionId = decodeMarkerPart(rawId);
    const name = decodeMarkerPart(rawName);
    return `[${sessionLinkLabel(sessionId, name)}](${sessionPath(sessionId)})`;
  });
}

export function compactLinkLabel(href: string, label?: string | null): string {
  const trimmedHref = href.trim();
  const trimmedLabel = label?.trim();
  if (!trimmedHref) return trimmedLabel || href;

  const parsedFile = parseMarkdownFileHref(trimmedHref);
  if (parsedFile) {
    const compact = `${pathBasename(parsedFile.path)}${focusSuffix(parsedFile.focus)}`;
    if (!trimmedLabel) return compact;
    const parsedLabel = parseMarkdownFileHref(trimmedLabel);
    if (parsedLabel && parsedLabel.path === parsedFile.path) return compact;
    if (parsedLabel && pathBasename(parsedLabel.path) === pathBasename(parsedFile.path)) return compact;
    if (normalizeComparableLink(trimmedLabel) === normalizeComparableLink(trimmedHref)) return compact;
    return trimmedLabel;
  }

  if (trimmedLabel && normalizeComparableLink(trimmedLabel) !== normalizeComparableLink(trimmedHref)) {
    return trimmedLabel || trimmedHref;
  }

  try {
    const url = new URL(trimmedHref);
    const path = decodeURIComponent(url.pathname).replace(TRAILING_SLASH_RE, "");
    const last = path.split("/").filter(Boolean).at(-1);
    if (last) return `${url.hostname}/${last}`;
    return url.hostname;
  } catch {
    return trimmedLabel || trimmedHref;
  }
}

/** Inline link standing in for a bcfile: reference. Rendered as a
 * <span role="link"> so it can nest inside an outer <button> (e.g.
 * tool-result-toggle) without producing invalid HTML — `<button>`
 * inside `<button>` breaks React hydration. Behaves like a button:
 * click + Enter/Space activate, focusable. */
function FileLinkButton({
  label,
  path,
  focus,
  onFileClick,
}: {
  label: ReactNode;
  path: string;
  focus?: FileFocus;
  onFileClick: (path: string, focus?: FileFocus) => void;
}) {
  const activate = (e: SyntheticEvent) => {
    e.stopPropagation();
    onFileClick(path, focus);
  };
  return (
    <span
      role="link"
      tabIndex={0}
      className="file-path-link"
      title={fileLinkTitle(path, focus)}
      onClick={activate}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          activate(e);
        }
      }}
    >
      <span className="file-path-link-icon" aria-hidden="true" />
      <span className="file-path-link-label">{label}</span>
    </span>
  );
}

function SessionLinkButton({
  sessionId,
  label,
}: {
  sessionId: string;
  label: ReactNode;
}) {
  const activate = (e: SyntheticEvent) => {
    e.stopPropagation();
    e.preventDefault();
    openSession(sessionId);
  };
  return (
    <span
      role="link"
      tabIndex={0}
      className="session-smart-link"
      onClick={activate}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") activate(e);
      }}
    >
      {label}
    </span>
  );
}

/** Walk a React tree replacing `bcfile:` markdown link tokens
 * (`[label](bcfile:...)`) inside text nodes with FileLinkButton. Uses
 * `[label](bcfile:href)` syntax found in raw text — used only for
 * non-markdown surfaces like collapsed previews and tool-result <pre>
 * blocks. Markdown surfaces use the `<a>` override in
 * `markdownLinkifyComponents` instead. */
const RAW_MARKDOWN_FILE_LINK_RE = /\[([^\]\n]+)\]\(([^)\s]+)\)/g;

function linkifyRawFileString(
  text: string,
  onFileClick?: (path: string, focus?: FileFocus) => void,
): ReactNode {
  if (!onFileClick) return text;
  const parts: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  const re = new RegExp(RAW_MARKDOWN_FILE_LINK_RE.source, "g");
  while ((m = re.exec(text)) !== null) {
    const [whole, label, href] = m;
    const start = m.index;
    if (start > last) parts.push(text.slice(last, start));
    const parsed = parseMarkdownFileHref(href);
    if (parsed) {
      const mediaType = getMediaType(parsed.path);
      if (mediaType) {
        parts.push(
          <MediaPreviewInline
            key={`media-${start}-${label}`}
            path={parsed.path}
            mediaType={mediaType}
            onFileClick={(p) => onFileClick(p, parsed.focus)}
          />,
        );
      } else {
        parts.push(
          <FileLinkButton
            key={`${start}-${label}`}
            label={compactLinkLabel(href, label)}
            path={parsed.path}
            focus={parsed.focus}
            onFileClick={onFileClick}
          />,
        );
      }
    } else {
      parts.push(whole);
    }
    last = start + whole.length;
  }
  if (last === 0) return text;
  if (last < text.length) parts.push(text.slice(last));
  return <>{parts}</>;
}

function linkifyRawString(
  text: string,
  onFileClick?: (path: string, focus?: FileFocus) => void,
): ReactNode {
  const parts: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  const re = new RegExp(SESSION_LINK_MARKER_RE.source, "g");
  while ((m = re.exec(text)) !== null) {
    const [whole, rawId, rawName] = m;
    const start = m.index;
    if (start > last) {
      parts.push(linkifyRawFileString(text.slice(last, start), onFileClick));
    }
    const sessionId = decodeMarkerPart(rawId);
    const name = decodeMarkerPart(rawName);
    parts.push(
      <SessionLinkButton
        key={`session-${start}-${sessionId}`}
        sessionId={sessionId}
        label={sessionLinkLabel(sessionId, name)}
      />,
    );
    last = start + whole.length;
  }
  if (last === 0) return linkifyRawFileString(text, onFileClick);
  if (last < text.length) parts.push(linkifyRawFileString(text.slice(last), onFileClick));
  return <>{parts}</>;
}

/** Walk a React node tree replacing bcfile: markdown-link tokens with
 * clickable buttons. Used by non-markdown renderers (raw <pre>, plain
 * text). Markdown renderers should use `markdownLinkifyComponents`. */
export function linkifyFilePaths(
  children: ReactNode,
  onFileClick?: (path: string, focus?: FileFocus) => void,
): ReactNode {
  if (children === null || children === undefined || typeof children === "boolean") {
    return children;
  }
  if (typeof children === "string") return linkifyRawString(children, onFileClick);
  if (typeof children === "number") return children;
  if (Array.isArray(children)) {
    return Children.map(children, (c, i) => (
      <Fragment key={i}>{linkifyFilePaths(c, onFileClick)}</Fragment>
    ));
  }
  if (isValidElement(children)) {
    const el = children as ReactElement<{ children?: ReactNode }>;
    if (el.type === "a") return el;
    const inner = el.props?.children;
    if (inner === undefined) return el;
    return cloneElement(el, undefined, linkifyFilePaths(inner, onFileClick));
  }
  return children;
}

/** Wraps `<table>` in a horizontally-scrollable container so wide tables
 *  don't clip on narrow viewports (mobile). */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function ScrollableTable({ children, node, ...props }: any) {
  void node;
  return (
    <div className="table-scroll-wrapper">
      <table {...props}>{children}</table>
    </div>
  );
}

/** Build a `components` map for react-markdown / @uiw/react-markdown-preview
 * that intercepts `<a href="bcfile:...">` produced by the backend's
 * file-ref resolver and renders them as clickable file buttons.
 * Always includes the scrollable-table override. */
export function markdownLinkifyComponents(
  onFileClick?: (path: string, focus?: FileFocus) => void,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
): Record<string, any> {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const components: Record<string, any> = { table: ScrollableTable };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  function Anchor({ href, children, ...props }: any) {
    const parsed = typeof href === "string" ? parseMarkdownFileHref(href) : null;
    const parsedSession = typeof href === "string" ? parseSessionHref(href) : null;
    if (parsedSession) {
      return (
        <SessionLinkButton
          sessionId={parsedSession.sessionId}
          label={plainText(children) ?? sessionLinkLabel(parsedSession.sessionId, "")}
        />
      );
    }
    if (parsed && onFileClick) {
      const mediaType = getMediaType(parsed.path);
      if (mediaType) {
        return (
          <MediaPreviewInline
            path={parsed.path}
            mediaType={mediaType}
            onFileClick={(p) => onFileClick(p, parsed.focus)}
          />
        );
      }
      return (
        <FileLinkButton
          label={compactLinkLabel(href, plainText(children))}
          path={parsed.path}
          focus={parsed.focus}
          onFileClick={onFileClick}
        />
      );
    }
    const fullHref = typeof href === "string" ? href : "";
    // In-page anchors (href="#…") are not navigable links.
    // rehype-autolink-headings injects one like this into every heading,
    // with an icon as its child. Render it as a plain anchor preserving
    // that child, instead of routing it through the external-link branch
    // below — which would replace the icon with the literal "#slug" text.
    if (fullHref.startsWith("#")) {
      return createElement("a", { href, ...props }, children);
    }
    return createElement(
      "a",
      {
        href,
        title: fullHref || undefined,
        rel: "noopener noreferrer",
        ...props,
        onClick: (e: SyntheticEvent) => {
          e.preventDefault();
          if (fullHref) openExternalLink(fullHref);
        },
      },
      fullHref ? compactLinkLabel(fullHref, plainText(children)) : children,
    );
  }
  components.a = Anchor;
  return components;
}
