import type { DroppedFile } from "@/lib/dropped-files";

/** One line of the review tree: a file, or a folder holding some. */
export interface PlanNode {
  name: string;
  /** Path relative to what was dropped — the same key the upload sends. */
  path: string;
  kind: "file" | "directory";
  /** Bytes: the file's own size, or everything under a folder. */
  size: number;
  /** Files: 1. Folders: how many files are under them. */
  count: number;
  children: PlanNode[];
}

/**
 * The tree behind an upload, built from the flat list the collector produced.
 *
 * A folder appears here only because a file under it does — an upload of a tree is
 * an upload of its files, and a folder holding nothing has nothing to send. So the
 * review shows exactly what will be created and no more.
 */
export function buildPlanTree(files: DroppedFile[]): PlanNode[] {
  const roots: PlanNode[] = [];
  const directories = new Map<string, PlanNode>();

  const directoryAt = (path: string, name: string, parent: PlanNode | null): PlanNode => {
    const existing = directories.get(path);
    if (existing) return existing;
    const node: PlanNode = { name, path, kind: "directory", size: 0, count: 0, children: [] };
    directories.set(path, node);
    (parent ? parent.children : roots).push(node);
    return node;
  };

  for (const item of files) {
    const parts = item.relativePath.split("/").filter(Boolean);
    if (parts.length === 0) continue;
    let parent: PlanNode | null = null;
    for (let index = 0; index < parts.length - 1; index += 1) {
      const path = parts.slice(0, index + 1).join("/");
      parent = directoryAt(path, parts[index], parent);
    }
    const leaf: PlanNode = {
      name: parts[parts.length - 1],
      path: item.relativePath,
      kind: "file",
      size: item.file.size,
      count: 1,
      children: [],
    };
    (parent ? parent.children : roots).push(leaf);
  }

  const total = (node: PlanNode): PlanNode => {
    if (node.kind === "file") return node;
    node.children = node.children.map(total);
    node.size = node.children.reduce((sum, child) => sum + child.size, 0);
    node.count = node.children.reduce((sum, child) => sum + child.count, 0);
    return node;
  };
  const sort = (nodes: PlanNode[]): PlanNode[] => {
    nodes.sort((a, b) =>
      a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === "directory" ? -1 : 1,
    );
    for (const node of nodes) sort(node.children);
    return nodes;
  };
  return sort(roots.map(total));
}

/** Whether *path* is excluded, itself or by a folder above it. */
export function isExcluded(path: string, excluded: Set<string>): boolean {
  if (excluded.has(path)) return true;
  for (const entry of excluded) {
    if (path.startsWith(`${entry}/`)) return true;
  }
  return false;
}

/** What the upload would actually send, given the exclusions. */
export function remainingFiles(files: DroppedFile[], excluded: Set<string>): DroppedFile[] {
  return files.filter((item) => !isExcluded(item.relativePath, excluded));
}

export function totalBytes(files: DroppedFile[]): number {
  return files.reduce((sum, item) => sum + item.file.size, 0);
}

/**
 * Add or remove *path* from the exclusions, keeping the set minimal.
 *
 * Re-including something inside an excluded folder has to drop that folder's
 * exclusion and re-exclude its other children, or the click would look ignored.
 */
export function toggleExclusion(
  path: string,
  excluded: Set<string>,
  tree: PlanNode[],
): Set<string> {
  const next = new Set(excluded);
  if (next.has(path)) {
    next.delete(path);
    return next;
  }
  if (!isExcluded(path, next)) {
    next.add(path);
    return next;
  }
  // Excluded by an ancestor: lift that, and push the exclusion down to the
  // siblings the operator did not just ask for.
  const ancestor = [...next].find((entry) => path.startsWith(`${entry}/`));
  if (ancestor === undefined) return next;
  next.delete(ancestor);
  const node = findNode(tree, ancestor);
  if (node) {
    for (const child of node.children) {
      if (path !== child.path && !path.startsWith(`${child.path}/`)) next.add(child.path);
    }
    // Walk down toward the re-included path, excluding aside-branches on the way.
    let cursor = node.children.find(
      (child) => path === child.path || path.startsWith(`${child.path}/`),
    );
    while (cursor && cursor.path !== path) {
      for (const child of cursor.children) {
        if (path !== child.path && !path.startsWith(`${child.path}/`)) next.add(child.path);
      }
      cursor = cursor.children.find(
        (child) => path === child.path || path.startsWith(`${child.path}/`),
      );
    }
  }
  return next;
}

function findNode(nodes: PlanNode[], path: string): PlanNode | null {
  for (const node of nodes) {
    if (node.path === path) return node;
    const found = findNode(node.children, path);
    if (found) return found;
  }
  return null;
}
