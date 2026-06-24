import { useState } from "react";
import type { FileNode } from "../types";

/**
 * Recursive tree-node renderer shared by the file and directory pickers.
 * The caller decides what selecting a node means (a file picker acts on
 * files, a directory picker on directories) — this component only reports
 * which node was selected/activated and renders the hierarchy.
 *
 * INVARIANT: when `forceExpanded` is true the node is rendered fully
 * expanded and its collapse toggle is inert — used for server-pruned
 * search results, where collapsing would hide matches.
 */
export function PickerNode({
  node,
  depth,
  selected,
  onSelect,
  onActivate,
  forceExpanded = false,
}: {
  node: FileNode;
  depth: number;
  selected: string;
  onSelect: (node: FileNode) => void;
  onActivate?: (node: FileNode) => void;
  forceExpanded?: boolean;
}) {
  const [open, setOpen] = useState(depth < 1);
  const expanded = forceExpanded || open;
  const isActive = node.path === selected;
  const pad = depth * 14 + 8;

  if (node.type === "file") {
    return (
      <div
        className={`fp-node fp-file ${isActive ? "fp-active" : ""}`}
        style={{ paddingInlineStart: pad }}
        onClick={() => onSelect(node)}
        onDoubleClick={() => onActivate?.(node)}
        title={node.path}
      >
        {node.name}
      </div>
    );
  }

  return (
    <div>
      <div
        className={`fp-node fp-dir ${isActive ? "fp-active" : ""}`}
        style={{ paddingInlineStart: pad }}
        onClick={() => {
          onSelect(node);
          if (!forceExpanded) setOpen((v) => !v);
        }}
        onDoubleClick={() => onActivate?.(node)}
        title={node.path}
      >
        <span className="fp-arrow">{expanded ? "▾" : "▸"}</span> {node.name}
      </div>
      {expanded &&
        node.children?.map((child) => (
          <PickerNode
            key={child.path}
            node={child}
            depth={depth + 1}
            selected={selected}
            onSelect={onSelect}
            onActivate={onActivate}
            forceExpanded={forceExpanded}
          />
        ))}
    </div>
  );
}
