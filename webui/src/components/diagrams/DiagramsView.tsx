import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ReactFlowProvider, useReactFlow, type Edge, type Node } from "@xyflow/react";
import { Check, ChevronDown, ChevronLeft, Code2, Download, Save, Waypoints } from "lucide-react";

import { ComponentPalette } from "./ComponentPalette";
import { DiagramCanvas, type DiagramSelection } from "./DiagramCanvas";
import { DiagramList } from "./DiagramList";
import { EdgeInspector, NodeInspector } from "./DiagramInspector";
import { diagramToText } from "./diagramToText";
import { exportDiagramImage } from "./exportDiagramImage";
import { ComponentCatalogProvider, useComponentCatalog } from "./useComponentCatalog";
import { useDiagrams } from "@/hooks/useDiagrams";
import { createBlankDiagram, type Diagram, type DiagramNodeData } from "./diagramTypes";

type ViewMode = "visual" | "code";

// Fake server inventory — the real list comes from the future Server
// Management module (ssh / ansible-runner / ssm / api backends).
const FAKE_SERVERS = ["prod-web-01", "prod-web-02", "staging-01"];

function toFlowNodes(diagram: Diagram): Node<DiagramNodeData>[] {
  return diagram.nodes.map((n) => ({
    id: n.id,
    position: n.position,
    data: n.data,
    type: n.type ?? "component",
    ...(n.parentId ? { parentId: n.parentId, extent: "parent" as const } : {}),
    ...(n.style ? { style: n.style } : {}),
  }));
}

function toFlowEdges(diagram: Diagram): Edge[] {
  // Label/line styling is centralized in DiagramCanvas's defaultEdgeOptions
  // so both seeded and hand-drawn edges render identically.
  //
  // sourceHandle/targetHandle must never be left undefined here: @xyflow's
  // own handle lookup treats a falsy handle id as "pick whichever handle of
  // that type was declared first" rather than "use the node's default"
  // (see DiagramNode.tsx) -- every saved edge predates giving Top/Bottom
  // real ids, so they all rely on "no handle specified" meaning the default
  // vertical pair. Substituting that pair explicitly here keeps every
  // previously-saved diagram rendering exactly as before.
  return diagram.edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    label: e.label,
    sourceHandle: e.sourceHandle ?? "bottom",
    targetHandle: e.targetHandle ?? "top",
  }));
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

// Two frames is the standard "wait for layout to settle" trick -- one for
// React to commit the DOM, one for the browser to actually paint/measure it
// -- so the canvas the Code view just switched away from has real dimensions
// by the time html-to-image reads it, instead of capturing it mid-transition.
function waitForNextPaint(): Promise<void> {
  return new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  });
}

function ExportButton({
  diagramName,
  mode,
  onSetMode,
}: {
  diagramName: string;
  mode: ViewMode;
  onSetMode: (mode: ViewMode) => void;
}) {
  // Requires a ReactFlowProvider ancestor -- reads the live canvas via
  // getNodes(), not diagramAsData, since the export captures the actual
  // rendered DOM (see exportDiagramImage.ts), not a re-derived model.
  const reactFlowInstance = useReactFlow();
  const [open, setOpen] = useState(false);
  const [exporting, setExporting] = useState(false);

  const handleExport = useCallback(
    async (format: "png" | "svg") => {
      setOpen(false);
      setExporting(true);
      try {
        // The Code view unmounts the canvas entirely (see the mode ternary
        // below), so there's nothing for html-to-image to capture from
        // there -- hop over to Visual just long enough to export, then
        // restore whichever view the user was actually on.
        const cameFromCode = mode === "code";
        if (cameFromCode) {
          onSetMode("visual");
          await waitForNextPaint();
        }
        await exportDiagramImage(reactFlowInstance, diagramName, format);
        if (cameFromCode) onSetMode("code");
      } finally {
        setExporting(false);
      }
    },
    [reactFlowInstance, diagramName, mode, onSetMode],
  );

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={exporting}
        className="flex h-8 items-center gap-1.5 rounded-full border border-border/45 bg-settings-surface px-3 text-[12px] font-medium text-foreground hover:bg-muted/70 disabled:cursor-not-allowed disabled:opacity-60"
      >
        <Download className="h-3.5 w-3.5" /> {exporting ? "Exporting…" : "Export"}
      </button>
      {open ? (
        <div className="absolute right-0 top-9 z-10 w-40 rounded-[14px] border border-border bg-settings-surface p-1.5 shadow-lg">
          <button
            type="button"
            onClick={() => void handleExport("png")}
            className="flex h-9 w-full items-center rounded-[10px] px-2.5 text-left text-[13px] font-medium text-foreground hover:bg-muted/70"
          >
            Download PNG
          </button>
          <button
            type="button"
            onClick={() => void handleExport("svg")}
            className="flex h-9 w-full items-center rounded-[10px] px-2.5 text-left text-[13px] font-medium text-foreground hover:bg-muted/70"
          >
            Download SVG
          </button>
        </div>
      ) : null}
    </div>
  );
}

interface DiagramEditorProps {
  diagram: Diagram;
  onBack: () => void;
  onSaved: (diagram: Diagram) => void;
  onSave: (diagram: Diagram) => Promise<Diagram>;
}

function DiagramEditor({ diagram, onBack, onSaved, onSave }: DiagramEditorProps) {
  const { componentTypes, findComponentType, groupType } = useComponentCatalog();
  const [mode, setMode] = useState<ViewMode>("visual");
  const [diagramId, setDiagramId] = useState(diagram.id);
  const [diagramName, setDiagramName] = useState(diagram.name);
  const [targets, setTargets] = useState<string[]>(diagram.targets);
  const [nodes, setNodes] = useState<Node<DiagramNodeData>[]>(() => toFlowNodes(diagram));
  const [edges, setEdges] = useState<Edge[]>(() => toFlowEdges(diagram));
  const [selection, setSelection] = useState<DiagramSelection>(null);
  const [lastSavedAt, setLastSavedAt] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  // A "Saved" state on the button itself, not just the small timestamp text
  // elsewhere in the header -- that text sits far from where a click lands,
  // easy to miss as confirmation the click actually did anything.
  const [justSaved, setJustSaved] = useState(false);
  const justSavedTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (justSavedTimeoutRef.current) clearTimeout(justSavedTimeoutRef.current);
    };
  }, []);

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
      id: diagramId,
      name: diagramName,
      targets,
      nodes: nodes.map((n) => ({
        id: n.id,
        position: n.position,
        data: n.data,
        type: n.type as "component" | "groupBox" | undefined,
        parentId: n.parentId,
        ...(typeof n.style?.width === "number" && typeof n.style?.height === "number"
          ? { style: { width: n.style.width, height: n.style.height } }
          : {}),
      })),
      edges: edges.map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        label: String(e.label ?? ""),
        sourceHandle: e.sourceHandle ?? undefined,
        targetHandle: e.targetHandle ?? undefined,
      })),
    }),
    [diagramId, diagramName, targets, nodes, edges],
  );

  const generatedText = useMemo(
    () => diagramToText(diagramAsData, componentTypes),
    [diagramAsData, componentTypes],
  );

  const handleSave = useCallback(async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const saved = await onSave(diagramAsData);
      setDiagramId(saved.id);
      setLastSavedAt(new Date().toLocaleTimeString());
      setJustSaved(true);
      if (justSavedTimeoutRef.current) clearTimeout(justSavedTimeoutRef.current);
      justSavedTimeoutRef.current = setTimeout(() => setJustSaved(false), 2000);
      onSaved(saved);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }, [diagramAsData, onSave, onSaved]);

  const handleToggleTarget = useCallback((server: string) => {
    setTargets((prev) => (prev.includes(server) ? prev.filter((s) => s !== server) : [...prev, server]));
  }, []);

  const handleAddComponent = useCallback(
    (componentTypeId: string, position?: { x: number; y: number }, parentId?: string) => {
      const id = `${componentTypeId}-${Math.random().toString(36).slice(2, 8)}`;
      const fallbackPosition = (offset: number) => ({ x: 120 + offset * 40, y: 80 + offset * 30 });
      const parentFields = parentId ? { parentId, extent: "parent" as const } : {};

      if (groupType && componentTypeId === groupType.id) {
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
            data: { label: "New Group", componentTypeId: groupType.id, providerId: "generic", config: {} },
            ...parentFields,
          },
        ]);
        setSelection({ kind: "node", id });
        return;
      }

      const type = findComponentType(componentTypeId);
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
    [groupType, findComponentType],
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
    <ReactFlowProvider>
      <div className="flex h-full min-h-0 w-full flex-col">
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onBack}
              aria-label="Back to diagrams"
              className="flex h-8 w-8 items-center justify-center rounded-full text-muted-foreground hover:bg-muted/70 hover:text-foreground"
            >
              <ChevronLeft className="h-4 w-4" />
            </button>
            <div className="flex flex-col">
              <input
                value={diagramName}
                onChange={(event) => setDiagramName(event.target.value)}
                placeholder="Untitled diagram"
                // Width tracks the title's own length (clamped) so it reads like
                // editable text, not a cramped fixed-size box — the browser
                // default (~20ch) truncated most real titles.
                style={{ width: `${Math.min(Math.max(diagramName.length, 10), 60) + 2}ch` }}
                className="-ml-1.5 max-w-full rounded-md border border-transparent bg-transparent px-1.5 py-0.5 text-[14px] font-semibold text-foreground outline-none hover:border-border/45 focus:border-border focus:bg-muted/70"
              />
              <span className="text-[11px] text-muted-foreground">
                {targets.length > 0 ? `Targets: ${targets.join(", ")}` : "No targets selected yet"}
                {lastSavedAt ? ` · Saved ${lastSavedAt}` : diagramId ? "" : " · Not saved yet"}
              </span>
              {saveError ? (
                <span className="text-[11px] text-destructive">Failed to save: {saveError}</span>
              ) : null}
            </div>
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
            <ExportButton diagramName={diagramName} mode={mode} onSetMode={setMode} />
            <button
              type="button"
              onClick={() => void handleSave()}
              disabled={saving}
              className={[
                "flex h-8 items-center gap-1.5 rounded-full border px-3 text-[12px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-60",
                justSaved
                  ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                  : "border-border/45 bg-settings-surface text-foreground hover:bg-muted/70",
              ].join(" ")}
            >
              {justSaved ? <Check className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />}
              {saving ? "Saving…" : justSaved ? "Saved" : "Save"}
            </button>
          </div>
        </div>

        {mode === "visual" ? (
          <div className="flex min-h-0 flex-1">
            <ComponentPalette onAdd={handleAddComponent} />
            <div className="min-h-0 flex-1">
              <DiagramCanvas
                nodes={nodes}
                edges={edges}
                onNodesChange={setNodes}
                onEdgesChange={setEdges}
                onSelect={setSelection}
                onDropComponent={handleAddComponent}
              />
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
            ) : null}
          </div>
        ) : (
          <div className="min-h-0 flex-1 overflow-auto bg-background p-4">
            <div className="mb-2 text-[11px] text-muted-foreground">
              Read-only preview generated from the visual diagram — switch back to Visual to edit. Includes the
              Mermaid frontmatter title, so this text is a complete, pasteable Mermaid diagram.
            </div>
            <pre className="rounded-[14px] border border-border/45 bg-settings-surface p-4 text-[12.5px] leading-relaxed text-foreground">
              <code>{generatedText}</code>
            </pre>
          </div>
        )}
      </div>
    </ReactFlowProvider>
  );
}

type Screen = { kind: "list" } | { kind: "loading" } | { kind: "editor"; diagram: Diagram };

interface DiagramsViewProps {
  // Which saved diagram (by UUID) the URL says should be open — lets a page
  // reload or a browser back/forward land back on the same diagram instead
  // of always dropping to the list.
  diagramId?: string | null;
  onDiagramIdChange?: (id: string | null, options?: { replace?: boolean }) => void;
}

/**
 * Diagrams persist through `/api/webui/diagrams*` (see `nanoinfra/diagrams/`)
 * via the `useDiagrams` hook — real, workspace-scoped storage, not a mock.
 */
export function DiagramsView({ diagramId = null, onDiagramIdChange }: DiagramsViewProps) {
  const { diagrams, load, save, remove, refresh } = useDiagrams();
  const [screen, setScreen] = useState<Screen>(diagramId ? { kind: "loading" } : { kind: "list" });

  // Tracks the id *we* last told the caller about (via onDiagramIdChange),
  // so the sync effect below can tell "the diagramId prop changed because
  // something outside us navigated (back/forward, a deep link)" apart from
  // "it changed because our own action just echoed back through the
  // caller's state" — without this, every internal open/save/back would
  // immediately be undone by the effect re-reading a diagramId prop that
  // hasn't caught up yet. Seeded with a sentinel (not `diagramId` itself) so
  // the effect also fires once on mount to fetch the initially-requested id.
  const lastEmittedIdRef = useRef<string | null | undefined>(undefined);

  useEffect(() => {
    if (diagramId === lastEmittedIdRef.current) return;
    lastEmittedIdRef.current = diagramId;
    if (!diagramId) {
      setScreen({ kind: "list" });
      return;
    }
    let cancelled = false;
    setScreen({ kind: "loading" });
    void load(diagramId).then((diagram) => {
      if (cancelled) return;
      setScreen(diagram ? { kind: "editor", diagram } : { kind: "list" });
    });
    return () => {
      cancelled = true;
    };
  }, [diagramId, load]);

  const handleNew = useCallback(async () => {
    // Persisted immediately (assigning its UUID right away) instead of
    // waiting for an explicit Save — otherwise the diagram has no id, so
    // there's nothing to put in the URL, and a reload while still editing a
    // brand-new diagram drops straight back to the list with it gone.
    const diagram = await save(createBlankDiagram());
    setScreen({ kind: "editor", diagram });
    lastEmittedIdRef.current = diagram.id;
    onDiagramIdChange?.(diagram.id);
  }, [save, onDiagramIdChange]);

  const handleOpen = useCallback(
    async (id: string) => {
      const diagram = await load(id);
      if (!diagram) return;
      setScreen({ kind: "editor", diagram });
      lastEmittedIdRef.current = id;
      onDiagramIdChange?.(id);
    },
    [load, onDiagramIdChange],
  );

  const handleDelete = useCallback(
    (id: string) => {
      remove(id).catch((e: unknown) => {
        console.error("Failed to delete diagram", e);
      });
    },
    [remove],
  );

  const handleBack = useCallback(() => {
    void refresh();
    setScreen({ kind: "list" });
    lastEmittedIdRef.current = null;
    onDiagramIdChange?.(null);
  }, [refresh, onDiagramIdChange]);

  const handleSaved = useCallback(
    (diagram: Diagram) => {
      // Re-key the editor with the assigned id so a second Save updates the
      // same record instead of minting a new UUID on every click.
      setScreen({ kind: "editor", diagram });
      // Only the *first* save (assigning a fresh id) is a real navigation —
      // the diagram becomes addressable at a URL that didn't exist before,
      // so it's pushed, not replaced. Skipping the call on every later save
      // (id unchanged) also avoids replacing the URL entry out from under
      // whatever the user already pushed since (e.g. having opened a
      // different diagram, then coming back and saving this one again).
      if (diagram.id !== lastEmittedIdRef.current) {
        lastEmittedIdRef.current = diagram.id;
        onDiagramIdChange?.(diagram.id);
      }
    },
    [onDiagramIdChange],
  );

  return (
    <ComponentCatalogProvider>
      {screen.kind === "loading" ? (
        <div className="flex h-full w-full items-center justify-center text-[13px] text-muted-foreground">
          Loading…
        </div>
      ) : screen.kind === "list" ? (
        <DiagramList diagrams={diagrams} onOpen={handleOpen} onNew={handleNew} onDelete={handleDelete} />
      ) : (
        <DiagramEditor
          key={screen.diagram.id || "new"}
          diagram={screen.diagram}
          onBack={handleBack}
          onSaved={handleSaved}
          onSave={save}
        />
      )}
    </ComponentCatalogProvider>
  );
}
