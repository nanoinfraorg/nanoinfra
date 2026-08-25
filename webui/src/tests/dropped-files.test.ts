import { describe, expect, it } from "vitest";

import { collectDroppedFiles, filesFromList } from "@/lib/dropped-files";

function file(name: string, relativePath?: string): File {
  const made = new File(["x"], name, { type: "text/plain" });
  if (relativePath !== undefined) {
    Object.defineProperty(made, "webkitRelativePath", { value: relativePath });
  }
  return made;
}

function fileEntry(name: string) {
  return {
    isFile: true,
    isDirectory: false,
    name,
    file: (onSuccess: (f: File) => void) => onSuccess(file(name)),
  };
}

/** A directory whose reader answers in batches, as the real one does. */
function directoryEntry(name: string, children: unknown[], batchSize = 100) {
  return {
    isFile: false,
    isDirectory: true,
    name,
    createReader: () => {
      let index = 0;
      return {
        readEntries: (onSuccess: (entries: unknown[]) => void) => {
          const batch = children.slice(index, index + batchSize);
          index += batch.length;
          onSuccess(batch);
        },
      };
    },
  };
}

function transfer(entries: unknown[], files: File[] = []): DataTransfer {
  return {
    items: entries.map((entry) => ({ webkitGetAsEntry: () => entry })),
    files: files as unknown as FileList,
  } as unknown as DataTransfer;
}

describe("collectDroppedFiles", () => {
  it("walks a dropped folder and keeps each file's path inside it", async () => {
    const dropped = directoryEntry("docs", [
      fileEntry("index.md"),
      directoryEntry("img", [fileEntry("logo.png")]),
    ]);

    const { files, truncated } = await collectDroppedFiles(transfer([dropped]));

    expect(files.map((f) => f.relativePath)).toEqual(["docs/index.md", "docs/img/logo.png"]);
    expect(truncated).toBe(false);
  });

  it("reads every batch a directory answers in", async () => {
    // `readEntries` returns at most 100 entries per call and ends with an empty
    // batch. Calling it once is how a folder upload silently loses files.
    const children = Array.from({ length: 250 }, (_, index) => fileEntry(`file-${index}.txt`));
    const dropped = directoryEntry("many", children);

    const { files } = await collectDroppedFiles(transfer([dropped]));

    expect(files).toHaveLength(250);
    expect(files[249].relativePath).toBe("many/file-249.txt");
  });

  it("keeps a plain dropped file at the top level", async () => {
    const { files } = await collectDroppedFiles(transfer([fileEntry("notes.md")]));

    expect(files).toEqual([expect.objectContaining({ relativePath: "notes.md" })]);
  });

  it("stops at the cap and says it did", async () => {
    const dropped = directoryEntry(
      "many",
      Array.from({ length: 10 }, (_, index) => fileEntry(`file-${index}.txt`)),
    );

    const { files, truncated } = await collectDroppedFiles(transfer([dropped]), 4);

    expect(files).toHaveLength(4);
    expect(truncated).toBe(true);
  });

  it("contributes nothing for an empty folder", async () => {
    const { files } = await collectDroppedFiles(transfer([directoryEntry("empty", [])]));

    expect(files).toEqual([]);
  });

  it("falls back to the file list where the entry API is missing", async () => {
    const plain = {
      items: [{}],
      files: [file("notes.md")] as unknown as FileList,
    } as unknown as DataTransfer;

    const { files } = await collectDroppedFiles(plain);

    expect(files.map((f) => f.relativePath)).toEqual(["notes.md"]);
  });
});

describe("filesFromList", () => {
  it("keeps the tree a directory pick reports", () => {
    const list = [
      file("index.md", "docs/index.md"),
      file("logo.png", "docs/img/logo.png"),
    ] as unknown as FileList;

    expect(filesFromList(list).files.map((f) => f.relativePath)).toEqual([
      "docs/index.md",
      "docs/img/logo.png",
    ]);
  });

  it("uses the name when there is no relative path", () => {
    const list = [file("notes.md")] as unknown as FileList;

    expect(filesFromList(list).files.map((f) => f.relativePath)).toEqual(["notes.md"]);
  });

  it("caps and reports", () => {
    const list = [file("a"), file("b"), file("c")] as unknown as FileList;

    const { files, truncated } = filesFromList(list, 2);

    expect(files).toHaveLength(2);
    expect(truncated).toBe(true);
  });
});
