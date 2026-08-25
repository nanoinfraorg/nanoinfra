import { describe, expect, it } from "vitest";

import {
  buildPlanTree,
  isExcluded,
  remainingFiles,
  toggleExclusion,
  totalBytes,
} from "@/components/workspace/uploadPlan";
import type { DroppedFile } from "@/lib/dropped-files";

function dropped(relativePath: string, size = 10): DroppedFile {
  return { relativePath, file: { size, name: relativePath.split("/").pop() } as File };
}

const TREE = [
  dropped("docs/index.md", 100),
  dropped("docs/img/logo.png", 400),
  dropped("docs/img/icon.png", 100),
  dropped("README.md", 50),
];

describe("buildPlanTree", () => {
  it("nests folders, folders first, and totals what is under them", () => {
    const tree = buildPlanTree(TREE);

    expect(tree.map((node) => node.name)).toEqual(["docs", "README.md"]);
    const docs = tree[0];
    expect(docs.count).toBe(3);
    expect(docs.size).toBe(600);
    expect(docs.children.map((child) => child.name)).toEqual(["img", "index.md"]);
    expect(docs.children[0].count).toBe(2);
    expect(docs.children[0].size).toBe(500);
  });

  it("holds no folder that carries no file", () => {
    // An upload of a tree is an upload of its files, so the review shows exactly
    // what will be created and no more.
    expect(buildPlanTree([]).length).toBe(0);
  });
});

describe("exclusions", () => {
  it("removing a folder removes everything under it", () => {
    const excluded = new Set(["docs/img"]);

    expect(isExcluded("docs/img/logo.png", excluded)).toBe(true);
    expect(isExcluded("docs/index.md", excluded)).toBe(false);
    expect(remainingFiles(TREE, excluded).map((f) => f.relativePath)).toEqual([
      "docs/index.md",
      "README.md",
    ]);
    expect(totalBytes(remainingFiles(TREE, excluded))).toBe(150);
  });

  it("putting one file back lifts its folder's exclusion and keeps the rest out", () => {
    // Otherwise the click looks ignored: the file is still covered by the folder.
    const tree = buildPlanTree(TREE);
    const excluded = toggleExclusion("docs/img/logo.png", new Set(["docs/img"]), tree);

    expect(isExcluded("docs/img/logo.png", excluded)).toBe(false);
    expect(isExcluded("docs/img/icon.png", excluded)).toBe(true);
    expect(remainingFiles(TREE, excluded).map((f) => f.relativePath)).toEqual([
      "docs/index.md",
      "docs/img/logo.png",
      "README.md",
    ]);
  });

  it("puts a whole subtree back when the folder itself is re-included", () => {
    const tree = buildPlanTree(TREE);
    const removed = toggleExclusion("docs", new Set(), tree);
    expect(remainingFiles(TREE, removed).map((f) => f.relativePath)).toEqual(["README.md"]);

    const restored = toggleExclusion("docs", removed, tree);
    expect(remainingFiles(TREE, restored)).toHaveLength(4);
  });

  it("re-including something two levels down keeps the branches beside it out", () => {
    const tree = buildPlanTree([...TREE, dropped("docs/img/deep/pixel.png", 5)]);
    const excluded = toggleExclusion("docs/img/deep/pixel.png", new Set(["docs"]), tree);

    expect(isExcluded("docs/img/deep/pixel.png", excluded)).toBe(false);
    expect(isExcluded("docs/index.md", excluded)).toBe(true);
    expect(isExcluded("docs/img/logo.png", excluded)).toBe(true);
    expect(isExcluded("README.md", excluded)).toBe(false);
  });
});
