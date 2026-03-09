import { useState } from "react";

function TreeNode({ node, level = 0, onSelect }) {
  const [open, setOpen] = useState(true);
  const hasChildren = node.children && node.children.length > 0;

  return (
    <div>
      <div
        style={{
          paddingLeft: `${level * 14}px`,
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          gap: "8px",
          paddingTop: "4px",
          paddingBottom: "4px",
        }}
        onClick={() => {
          if (hasChildren) setOpen(!open);
          onSelect(node);
        }}
      >
        <span>{hasChildren ? (open ? "▾" : "▸") : "•"}</span>
        <span>{node.name}</span>
        {node.current_uid ? (
          <span style={{ fontSize: "12px", opacity: 0.7 }}>
            current: {node.current_uid}
          </span>
        ) : null}
      </div>

      {hasChildren && open
        ? node.children.map((child, idx) => (
            <TreeNode
              key={`${child.name}-${idx}`}
              node={child}
              level={level + 1}
              onSelect={onSelect}
            />
          ))
        : null}
    </div>
  );
}

export default function VaultBrowser({ tree }) {
  const [selected, setSelected] = useState(null);

  return (
    <div className="rounded-2xl border bg-white shadow p-4 h-full flex gap-4">
      <div className="w-1/2 overflow-auto border-r pr-3">
        <h3 className="font-semibold mb-3">Vault Browser</h3>
        {tree ? <TreeNode node={tree} onSelect={setSelected} /> : <div>No data</div>}
      </div>

      <div className="w-1/2 overflow-auto pl-3">
        <h3 className="font-semibold mb-3">Details</h3>
        {selected ? (
          <pre className="text-xs whitespace-pre-wrap">
            {JSON.stringify(selected, null, 2)}
          </pre>
        ) : (
          <div>Select a node</div>
        )}
      </div>
    </div>
  );
}
