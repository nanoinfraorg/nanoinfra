import { Lock, Trash2, Unlock, X } from "lucide-react";
import type { Edge, Node } from "@xyflow/react";

import { findComponentType, findProvider } from "./componentCatalog";
import type { DiagramNodeData } from "./diagramTypes";

interface NodeInspectorProps {
  node: Node<DiagramNodeData>;
  onClose: () => void;
  onChangeLabel: (value: string) => void;
  onChangeProvider: (providerId: string) => void;
  onChangeConfig: (key: string, value: string) => void;
  onToggleLock: () => void;
  onDelete: () => void;
}

interface EdgeInspectorProps {
  edge: Edge;
  sourceLabel: string;
  targetLabel: string;
  onClose: () => void;
  onChangeLabel: (value: string) => void;
  onDelete: () => void;
}

function InspectorShell({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="flex h-full w-[280px] shrink-0 flex-col gap-4 overflow-y-auto border-l border-border bg-settings-surface px-4 py-4">
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          {title}
        </span>
        <button
          type="button"
          onClick={onClose}
          className="touch-target flex h-7 w-7 items-center justify-center rounded-full text-muted-foreground hover:bg-muted/70 hover:text-foreground"
          aria-label="Close inspector"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      {children}
    </div>
  );
}

function ActionRow({
  locked,
  onToggleLock,
  onDelete,
}: {
  locked?: boolean;
  onToggleLock?: () => void;
  onDelete: () => void;
}) {
  return (
    <div className="flex items-center gap-2 border-t border-border/45 pt-3">
      {onToggleLock ? (
        <button
          type="button"
          onClick={onToggleLock}
          className="touch-target flex h-9 flex-1 items-center justify-center gap-1.5 rounded-[10px] border border-border/45 text-[12.5px] font-medium text-foreground hover:bg-muted/70"
        >
          {locked ? <Unlock className="h-3.5 w-3.5" /> : <Lock className="h-3.5 w-3.5" />}
          {locked ? "Unlock" : "Lock"}
        </button>
      ) : null}
      <button
        type="button"
        onClick={onDelete}
        disabled={locked}
        className="touch-target flex h-9 flex-1 items-center justify-center gap-1.5 rounded-[10px] border border-destructive/25 text-[12.5px] font-medium text-destructive hover:bg-destructive/8 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
      >
        <Trash2 className="h-3.5 w-3.5" />
        Delete
      </button>
    </div>
  );
}

export function NodeInspector({
  node,
  onClose,
  onChangeLabel,
  onChangeProvider,
  onChangeConfig,
  onToggleLock,
  onDelete,
}: NodeInspectorProps) {
  const isGroup = node.type === "group";
  const type = findComponentType(node.data.componentTypeId);
  const provider = findProvider(node.data.componentTypeId, node.data.providerId);

  return (
    <InspectorShell title="Inspector" onClose={onClose}>
      <label className="flex flex-col gap-1">
        <span className="text-[12px] font-medium text-muted-foreground">
          {isGroup ? "Name" : "Label"}
        </span>
        <input
          value={node.data.label}
          disabled={node.data.locked}
          onChange={(event) => onChangeLabel(event.target.value)}
          placeholder={isGroup ? "e.g. Kubernetes Cluster, Scaling Group" : undefined}
          className="h-9 rounded-[10px] border border-border/45 bg-background px-2.5 text-[13px] text-foreground outline-none focus-visible:border-border disabled:opacity-60"
        />
      </label>

      {isGroup ? (
        <span className="text-[11px] text-muted-foreground">
          Drag components — or other groups — onto this box to nest them inside it. Connect it to
          other things on the canvas like any other component.
        </span>
      ) : null}

      <label className="flex flex-col gap-1">
        <span className="text-[12px] font-medium text-muted-foreground">Provider</span>
        <select
          value={node.data.providerId}
          disabled={node.data.locked}
          onChange={(event) => onChangeProvider(event.target.value)}
          className="h-9 rounded-[10px] border border-border/45 bg-background px-2.5 text-[13px] text-foreground outline-none focus-visible:border-border disabled:opacity-60"
        >
          {type?.providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </select>
        {provider && !isGroup ? (
          <span className="text-[11px] text-muted-foreground">
            kind: <code className="rounded bg-muted px-1 py-0.5">{provider.kind}</code>
          </span>
        ) : null}
      </label>

      {provider?.fields.map((field) => (
        <label key={field.key} className="flex flex-col gap-1">
          <span className="text-[12px] font-medium text-muted-foreground">{field.label}</span>
          <input
            type={field.kind === "secret" ? "password" : "text"}
            disabled={node.data.locked}
            value={node.data.config[field.key] ?? ""}
            placeholder={field.kind === "secret" ? "stored in Secrets Manager" : field.placeholder}
            onChange={(event) => onChangeConfig(field.key, event.target.value)}
            className="h-9 rounded-[10px] border border-border/45 bg-background px-2.5 text-[13px] text-foreground outline-none focus-visible:border-border disabled:opacity-60"
          />
        </label>
      ))}

      <ActionRow locked={node.data.locked} onToggleLock={onToggleLock} onDelete={onDelete} />
    </InspectorShell>
  );
}

export function EdgeInspector({ edge, sourceLabel, targetLabel, onClose, onChangeLabel, onDelete }: EdgeInspectorProps) {
  return (
    <InspectorShell title="Connection" onClose={onClose}>
      <div className="flex flex-col gap-1 text-[12.5px]">
        <span className="text-foreground">{sourceLabel}</span>
        <span className="text-muted-foreground">↓</span>
        <span className="text-foreground">{targetLabel}</span>
      </div>

      <label className="flex flex-col gap-1">
        <span className="text-[12px] font-medium text-muted-foreground">Relationship</span>
        <input
          value={typeof edge.label === "string" ? edge.label : ""}
          onChange={(event) => onChangeLabel(event.target.value)}
          placeholder="e.g. Route HTTP, Read/Write, Replicate"
          className="h-9 rounded-[10px] border border-border/45 bg-background px-2.5 text-[13px] text-foreground outline-none focus-visible:border-border"
        />
        <span className="text-[11px] text-muted-foreground">
          Free text — pre-filled with a guess based on the two components, always editable.
        </span>
      </label>

      <ActionRow onDelete={onDelete} />
    </InspectorShell>
  );
}

export function EmptyInspector() {
  return (
    <div className="flex h-full w-[280px] shrink-0 flex-col items-center justify-center gap-1 border-l border-border bg-settings-surface px-4 text-center">
      <span className="text-[13px] font-medium text-foreground">Nothing selected</span>
      <span className="text-[12px] text-muted-foreground">
        Click a component or connection on the canvas to configure it.
      </span>
    </div>
  );
}
