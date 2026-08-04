import { useCallback, useMemo, useState } from "react";
import { ReactFlowProvider, type Edge, type Node } from "@xyflow/react";
import { Check, ChevronDown, Code2, Waypoints } from "lucide-react";

import { ComponentPalette } from "./ComponentPalette";
import { DiagramCanvas, type DiagramSelection } from "./DiagramCanvas";
import { EdgeInspector, EmptyInspector, NodeInspector } from "./DiagramInspector";
import { diagramToText } from "./diagramToText";
import { COMPONENT_TYPES, GROUP_COMPONENT_ID } from "./componentCatalog";
import { SEED_DIAGRAM } from "./seedDiagram";
import type { Diagram, DiagramNodeData } from "./diagramTypes";

type ViewMode = "visual" | "code";

// Fake server inventory for the mock — the real list comes from the future
// Server Management module (ssh / ansible-runner / ssm / api backends).
const FAKE_SERVERS = ["prod-web-01", "prod-web-02", "staging-01"];

function toFlowNodes(diagram: Diagram): Node<DiagramNodeData>[] {
  return diagram.nodes.map((n) => ({ id: n.id, position: n.position, data: n.data, type: "component" }));
}

function toFlowEdges(diagram: Diagram): Edge[] {
  // Label/line styling is centralized in DiagramCanvas's defaultEdgeOptions
  // so both seeded and hand-drawn edges render identically.
  return diagram.edges.map((e) => ({ id: e.id, source: e.source, target: e.target, label: e.label }));
}

function TargetPicker({
  targets,
  onToggle,
}: {
  targets: string[];
  onToggle: (server: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex h-8 items-center gap-1.5 rounded-full border border-border/45 px-3 text-[12px] font-medium text-foreground hover:bg-muted/70"
      >
        {targets.length > 0 ? `${targets.length} target${targets.length > 1 ? "s" : ""}` : "Select target(s)"}
        <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
      </button>
      {open ? (
        <div className="absolute right-0 top-9 z-10 w-56 rounded-[14px] border border-border bg-settings-surface p-1.5 shadow-lg">
          {FAKE_SERVERS.map((server) => {
            const active = targets.includes(server);
            return (
              <button
                key={server}
                type="button"
                onClick={() => onToggle(server)}
                className="flex h-9 w-full items-center justify-between rounded-[10px] px-2.5 text-left text-[13px] font-medium text-foreground hover:bg-muted/70"
              >
                {server}
                {active ? <Check className="h-3.5 w-3.5" /> : null}
              </button>
            );
          })}
          <div className="mt-1 border-t border-border/45 px-2.5 pt-2 text-[11px] text-muted-foreground">
            Fake list for now — real servers come from Server Management.
          </div>
        </div>
      ) : null}
    </div>
  );
}

/**
 * Prototype only: fake/local data, no backend calls, no persistence.
 * Validates the Visual (canvas + inspector) / Code (generated text) UX
 * before the real Component/Provider model and agent tools exist.
 */
export function DiagramsView() {
  const [mode, setMode] = useState<ViewMode>("visual");
  const [diagramName] = useState(SEED_DIAGRAM.name);
  const [targets, setTargets] = useState<string[]>(SEED_DIAGRAM.targets);
  const [nodes, setNodes] = useState<Node<DiagramNodeData>[]>(() => toFlowNodes(SEED_DIAGRAM));
  const [edges, setEdges] = useState<Edge[]>(() => toFlowEdges(SEED_DIAGRAM));
  const [selection, setSelection] = useState<DiagramSelection>(null);

  const selectedNode = useMemo(
    () => (selection?.kind === "node" ? nodes.find((n) => n.id === selection.id) ?? null : null),
    [nodes, selection],
  );
  const selectedEdge = useMemo(
    () => (selection?.kind === "edge" ? edges.find((e) => e.id === selection.id) ?? null : null),
    [edges, selection],
  );

  const diagramAsData = useMemo<Diagram>(
    () => ({
      id: "prototype",
      name: diagramName,
      targets,
      nodes: nodes.map((n) => ({ id: n.id, position: n.position, data: n.data })),
      edges: edges.map((e) => ({ id: e.id, source: e.source, target: e.target, label: String(e.label ?? "") })),
    }),
    [diagramName, targets, nodes, edges],
  );

  const generatedText = useMemo(() => diagramToText(diagramAsData), [diagramAsData]);

  const handleToggleTarget = useCallback((server: string) => {
    setTargets((prev) => (prev.includes(server) ? prev.filter((s) => s !== server) : [...prev, server]));
  }, []);

  const handleAddComponent = useCallback(
    (componentTypeId: string, position?: { x: number; y: number }, parentId?: string) => {
      const id = `${componentTypeId}-${Math.random().toString(36).slice(2, 8)}`;
      const fallbackPosition = (offset: number) => ({ x: 120 + offset * 40, y: 80 + offset * 30 });
      const parentFields = parentId ? { parentId, extent: "parent" as const } : {};

      if (componentTypeId === GROUP_COMPONENT_ID) {
        // A group nested inside another group defaults smaller than its
        // parent's own default size, so it visually fits instead of
        // overflowing and overlapping siblings — the outer size is roomy
        // enough for several top-level components.
        const size = parentId ? { width: 220, height: 150 } : { width: 320, height: 220 };
        setNodes((prev) => [
          ...prev,
          {
            id,
            type: "groupBox",
            position: position ?? fallbackPosition(prev.length),
            style: size,
            data: { label: "New Group", componentTypeId: GROUP_COMPONENT_ID, providerId: "generic", config: {} },
            ...parentFields,
          },
        ]);
        setSelection({ kind: "node", id });
        return;
      }

      const type = COMPONENT_TYPES.find((c) => c.id === componentTypeId);
      if (!type) return;
      setNodes((prev) => [
        ...prev,
        {
          id,
          type: "component",
          position: position ?? fallbackPosition(prev.length),
          data: {
            label: type.label,
            componentTypeId: type.id,
            providerId: type.providers[0]?.id ?? "",
            config: {},
          },
          ...parentFields,
        },
      ]);
      setSelection({ kind: "node", id });
    },
    [],
  );

  const updateSelectedNode = useCallback(
    (updater: (data: DiagramNodeData) => DiagramNodeData) => {
      if (selection?.kind !== "node") return;
      setNodes((prev) => prev.map((n) => (n.id === selection.id ? { ...n, data: updater(n.data) } : n)));
    },
    [selection],
  );

  const handleToggleLock = useCallback(() => {
    updateSelectedNode((data) => ({ ...data, locked: !data.locked }));
  }, [updateSelectedNode]);

  const handleDeleteNode = useCallback(() => {
    if (selection?.kind !== "node" || selectedNode?.data.locked) return;
    const id = selection.id;
    const groupBeingDeleted = selectedNode?.type === "groupBox" ? selectedNode : undefined;
    setNodes((prev) =>
      prev
        .filter((n) => n.id !== id)
        // Deleting a group ungroups its children instead of deleting them —
        // they move up exactly one level, to the deleted group's own parent
        // (or the top level, if it had none), rather than being flattened
        // all the way to the canvas root when the group was nested.
        .map((n) =>
          groupBeingDeleted && n.parentId === id
            ? {
                ...n,
                parentId: groupBeingDeleted.parentId,
                extent: groupBeingDeleted.parentId ? ("parent" as const) : undefined,
                position: {
                  x: n.position.x + groupBeingDeleted.position.x,
                  y: n.position.y + groupBeingDeleted.position.y,
                },
              }
            : n,
        ),
    );
    setEdges((prev) => prev.filter((e) => e.source !== id && e.target !== id));
    setSelection(null);
  }, [selection, selectedNode]);

  const handleChangeEdgeLabel = useCallback(
    (value: string) => {
      if (selection?.kind !== "edge") return;
      setEdges((prev) => prev.map((e) => (e.id === selection.id ? { ...e, label: value } : e)));
    },
    [selection],
  );

  const handleDeleteEdge = useCallback(() => {
    if (selection?.kind !== "edge") return;
    setEdges((prev) => prev.filter((e) => e.id !== selection.id));
    setSelection(null);
  }, [selection]);

  return (
    <div className="flex h-full min-h-0 w-full flex-col">
      <div className="flex items-center justify-between border-b border-border px-4 py-3">
        <div className="flex flex-col">
          <span className="text-[14px] font-semibold text-foreground">{diagramName}</span>
          <span className="text-[11px] text-muted-foreground">
            {targets.length > 0 ? `Targets: ${targets.join(", ")}` : "No targets selected yet"}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <TargetPicker targets={targets} onToggle={handleToggleTarget} />
          <div className="flex items-center gap-0.5 rounded-full bg-muted p-0.5 text-[12px] font-medium text-muted-foreground">
            <button
              type="button"
              onClick={() => setMode("visual")}
              className={[
                "flex items-center gap-1.5 rounded-full px-3 py-1 transition-colors",
                mode === "visual" ? "bg-background text-foreground" : "hover:text-foreground",
              ].join(" ")}
            >
              <Waypoints className="h-3.5 w-3.5" /> Visual
            </button>
            <button
              type="button"
              onClick={() => setMode("code")}
              className={[
                "flex items-center gap-1.5 rounded-full px-3 py-1 transition-colors",
                mode === "code" ? "bg-background text-foreground" : "hover:text-foreground",
              ].join(" ")}
            >
              <Code2 className="h-3.5 w-3.5" /> Code
            </button>
          </div>
        </div>
      </div>

      {mode === "visual" ? (
        <div className="flex min-h-0 flex-1">
          <ComponentPalette onAdd={handleAddComponent} />
          <div className="min-h-0 flex-1">
            <ReactFlowProvider>
              <DiagramCanvas
                nodes={nodes}
                edges={edges}
                onNodesChange={setNodes}
                onEdgesChange={setEdges}
                onSelect={setSelection}
                onDropComponent={handleAddComponent}
              />
            </ReactFlowProvider>
          </div>
          {selectedNode ? (
            <NodeInspector
              node={selectedNode}
              nodes={nodes}
              edges={edges}
              onClose={() => setSelection(null)}
              onChangeLabel={(value) => updateSelectedNode((data) => ({ ...data, label: value }))}
              onChangeProvider={(providerId) => updateSelectedNode((data) => ({ ...data, providerId, config: {} }))}
              onChangeConfig={(key, value) =>
                updateSelectedNode((data) => ({ ...data, config: { ...data.config, [key]: value } }))
              }
              onToggleLock={handleToggleLock}
              onDelete={handleDeleteNode}
            />
          ) : selectedEdge ? (
            <EdgeInspector
              edge={selectedEdge}
              sourceLabel={nodes.find((n) => n.id === selectedEdge.source)?.data.label ?? selectedEdge.source}
              targetLabel={nodes.find((n) => n.id === selectedEdge.target)?.data.label ?? selectedEdge.target}
              onClose={() => setSelection(null)}
              onChangeLabel={handleChangeEdgeLabel}
              onDelete={handleDeleteEdge}
            />
          ) : (
            <EmptyInspector />
          )}
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-auto bg-background p-4">
          <div className="mb-2 text-[11px] text-muted-foreground">
            Read-only preview generated from the visual diagram — switch back to Visual to edit.
          </div>
          <pre className="rounded-[14px] border border-border/45 bg-settings-surface p-4 text-[12.5px] leading-relaxed text-foreground">
            <code>{generatedText}</code>
          </pre>
        </div>
      )}
    </div>
  );
}
