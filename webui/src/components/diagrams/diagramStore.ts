import type { Diagram } from "./diagramTypes";
import { SEED_DIAGRAM } from "./seedDiagram";

/**
 * Prototype persistence: localStorage stands in for the future backend
 * (a real diagrams API). Diagrams are keyed by UUID, assigned the first
 * time a diagram is saved — a diagram that only exists in the editor and
 * has never been saved has no id yet.
 */
const STORAGE_KEY = "nanoinfra-webui.diagrams";

export interface DiagramRecord extends Diagram {
  updatedAt: string;
}

export interface DiagramSummary {
  id: string;
  name: string;
  targets: string[];
  nodeCount: number;
  updatedAt: string;
}

interface StoredState {
  diagrams: Record<string, DiagramRecord>;
}

function readState(): StoredState {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return { diagrams: { [SEED_DIAGRAM.id]: seedRecord() } };
    const parsed = JSON.parse(raw) as Partial<StoredState>;
    return { diagrams: parsed.diagrams ?? {} };
  } catch {
    return { diagrams: { [SEED_DIAGRAM.id]: seedRecord() } };
  }
}

function seedRecord(): DiagramRecord {
  return { ...SEED_DIAGRAM, updatedAt: new Date(0).toISOString() };
}

function writeState(state: StoredState): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Mock persistence is best-effort only — losing it just means the
    // prototype forgets diagrams on next load, not a real data loss.
  }
}

export function listDiagrams(): DiagramSummary[] {
  const state = readState();
  return Object.values(state.diagrams)
    .map((d) => ({
      id: d.id,
      name: d.name,
      targets: d.targets,
      nodeCount: d.nodes.length,
      updatedAt: d.updatedAt,
    }))
    .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
}

export function loadDiagram(id: string): Diagram | null {
  const state = readState();
  const record = state.diagrams[id];
  if (!record) return null;
  const { updatedAt, ...diagram } = record;
  void updatedAt;
  return diagram;
}

/** Assigns a UUID the first time a diagram is saved; keeps it on every save after that. */
export function saveDiagram(diagram: Diagram): Diagram {
  const state = readState();
  const id = diagram.id || crypto.randomUUID();
  const saved: Diagram = { ...diagram, id };
  state.diagrams[id] = { ...saved, updatedAt: new Date().toISOString() };
  writeState(state);
  return saved;
}

export function deleteDiagram(id: string): void {
  const state = readState();
  delete state.diagrams[id];
  writeState(state);
}

export function createBlankDiagram(): Diagram {
  return { id: "", name: "Untitled diagram", targets: [], nodes: [], edges: [] };
}
