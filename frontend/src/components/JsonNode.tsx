import { useState } from "react";

/** If a string looks like embedded JSON (starts with { or [), try to parse
 *  it so we can render it with a nested JsonNode instead of a raw string. */
function tryParseEmbeddedJson(value: string): unknown | null {
  const trimmed = value.trim();
  if (trimmed.length < 2) return null;
  if (!/^[{[]/.test(trimmed)) return null;
  try {
    const parsed = JSON.parse(trimmed);
    if (parsed && typeof parsed === "object") return parsed;
  } catch (err) {
    void err;
  }
  return null;
}

function JsonString({ keyName, value }: { keyName?: string; value: string }) {
  const [expanded, setExpanded] = useState(false);
  const embedded = tryParseEmbeddedJson(value);
  if (embedded !== null) {
    return (
      <div className="json-node json-embedded">
        {keyName && <span className="json-key">"{keyName}": </span>}
        <span className="json-embedded-tag" title="Embedded JSON string">json</span>
        <JsonNode value={embedded} defaultOpen />
      </div>
    );
  }
  const multiline = value.includes("\n");
  const long = value.length > 120;
  const needsToggle = multiline || long;

  if (!needsToggle) {
    return (
      <span className="json-line">
        {keyName && <span className="json-key">"{keyName}": </span>}
        <span className="json-str">"{value}"</span>
      </span>
    );
  }

  if (!expanded) {
    const preview = multiline ? value.split("\n")[0] : value.slice(0, 120);
    return (
      <span className="json-line">
        {keyName && <span className="json-key">"{keyName}": </span>}
        <span
          className="json-str json-str-expandable"
          onClick={(e) => { e.stopPropagation(); setExpanded(true); }}
          title="Click to expand"
        >
          "{preview}<span className="json-str-more">\u2026</span>"
        </span>
      </span>
    );
  }

  return (
    <div className="json-node">
      <span
        className="json-toggle"
        onClick={(e) => { e.stopPropagation(); setExpanded(false); }}
      >
        <span className="json-arrow">{"\u25BC"}</span>
        {keyName && <span className="json-key">"{keyName}": </span>}
        <span className="json-str-brace">"</span>
      </span>
      <pre className="json-str-full">{value}</pre>
      <span className="json-str-brace">"</span>
    </div>
  );
}

export function JsonNode({ keyName, value, defaultOpen = false }: {
  keyName?: string;
  value: unknown;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);

  if (value === null) return <span className="json-line">{keyName && <span className="json-key">"{keyName}": </span>}<span className="json-null">null</span></span>;
  if (typeof value === "boolean") return <span className="json-line">{keyName && <span className="json-key">"{keyName}": </span>}<span className="json-bool">{String(value)}</span></span>;
  if (typeof value === "number") return <span className="json-line">{keyName && <span className="json-key">"{keyName}": </span>}<span className="json-num">{value}</span></span>;
  if (typeof value === "string") {
    return <JsonString keyName={keyName} value={value} />;
  }

  const isArray = Array.isArray(value);
  const entries = isArray ? (value as unknown[]).map((v, i) => [String(i), v] as const) : Object.entries(value as Record<string, unknown>);
  const openBrace = isArray ? "[" : "{";
  const closeBrace = isArray ? "]" : "}";

  if (entries.length === 0) {
    return <span className="json-line">{keyName && <span className="json-key">"{keyName}": </span>}{openBrace}{closeBrace}</span>;
  }

  return (
    <div className="json-node">
      <span className="json-toggle" onClick={() => setOpen(o => !o)}>
        <span className="json-arrow">{open ? "\u25BC" : "\u25B6"}</span>
        {keyName && <span className="json-key">"{keyName}": </span>}
        {openBrace}
        {!open && <span className="json-preview"> {entries.length} {isArray ? "items" : "keys"} {closeBrace}</span>}
      </span>
      {open && (
        <div className="json-children">
          {entries.map(([k, v]) => (
            <JsonNode key={k} keyName={isArray ? undefined : k} value={v} />
          ))}
        </div>
      )}
      {open && <span className="json-brace">{closeBrace}</span>}
    </div>
  );
}
