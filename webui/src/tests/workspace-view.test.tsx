import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WorkspaceView } from "@/components/workspace/WorkspaceView";
import * as api from "@/lib/api";
import { ApiError } from "@/lib/api";
import type { WorkspaceEntry, WorkspaceListingPayload } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchWorkspaceListing: vi.fn(),
    fetchWorkspaceFilePreview: vi.fn(),
    createWorkspaceFolder: vi.fn(),
    renameWorkspaceEntry: vi.fn(),
    moveWorkspaceEntry: vi.fn(),
    deleteWorkspaceEntry: vi.fn(),
    downloadWorkspaceFile: vi.fn(),
  };
});

const uploadSpy = vi.fn();

function fakeClient() {
  return {
    status: "open" as const,
    defaultChatId: null as string | null,
    onStatus: () => () => {},
    onError: () => () => {},
    onChat: () => () => {},
    onSessionUpdate: () => () => {},
    onDiagramUpdate: () => () => {},
    getRunStartedAt: () => null,
    uploadWorkspaceFile: uploadSpy,
    sendMessage: vi.fn(),
    newChat: vi.fn(),
    attach: vi.fn(),
    connect: vi.fn(),
    close: vi.fn(),
    updateUrl: vi.fn(),
  };
}

function wrap(children: ReactNode) {
  return (
    <ClientProvider
      client={fakeClient() as unknown as import("@/lib/nanoinfra-client").NanoinfraClient}
      token="tok"
    >
      {children}
    </ClientProvider>
  );
}

const ROOT = "/w";

function entry(overrides: Partial<WorkspaceEntry> & { name: string }): WorkspaceEntry {
  return {
    kind: "file",
    size: 12,
    modified: "2026-08-19T10:00:00+00:00",
    escapesWorkspace: false,
    ...overrides,
  };
}

function listing(
  path: string,
  entries: WorkspaceEntry[],
  overrides: Partial<WorkspaceListingPayload> = {},
): WorkspaceListingPayload {
  return {
    path,
    displayPath: path === ROOT ? "" : path.slice(ROOT.length + 1),
    projectPath: ROOT,
    parent: path === ROOT ? null : path.slice(0, path.lastIndexOf("/")) || ROOT,
    entries,
    truncated: false,
    includeHidden: false,
    hiddenCount: 0,
    ...overrides,
  };
}

const listSpy = vi.mocked(api.fetchWorkspaceListing);

/** One listing per directory, so expanding a folder gets its own answer. */
function serve(directories: Record<string, WorkspaceListingPayload>): void {
  listSpy.mockImplementation(async (_token, path) => {
    const found = directories[path ?? ROOT];
    if (!found) throw new ApiError(404, "path not found");
    return found;
  });
}

const ROOT_WITH_SRC = {
  [ROOT]: listing(ROOT, [
    entry({ name: "src", kind: "directory", size: null }),
    entry({ name: "README.md" }),
  ]),
  [`${ROOT}/src`]: listing(`${ROOT}/src`, [entry({ name: "main.py" })]),
};

function dataTransfer(
  overrides: Partial<DataTransfer> & { types: string[]; items?: unknown[] },
) {
  const store = new Map<string, string>();
  return {
    setData: (type: string, value: string) => store.set(type, value),
    getData: (type: string) => store.get(type) ?? "",
    files: [] as unknown as FileList,
    items: [] as unknown[],
    dropEffect: "none",
    effectAllowed: "none",
    ...overrides,
  } as unknown as DataTransfer;
}

/** A dropped file, as `webkitGetAsEntry` reports one. */
function fileEntry(name: string, size?: number) {
  return {
    isFile: true,
    isDirectory: false,
    name,
    file: (onSuccess: (f: File) => void) => {
      const made = new File(["body"], name, { type: "text/plain" });
      if (size !== undefined) Object.defineProperty(made, "size", { value: size });
      onSuccess(made);
    },
  };
}

/** A dropped directory, as `webkitGetAsEntry` reports one. */
function directoryEntry(name: string, children: unknown[]) {
  return {
    isFile: false,
    isDirectory: true,
    name,
    createReader: () => {
      let sent = false;
      return {
        readEntries: (onSuccess: (entries: unknown[]) => void) => {
          onSuccess(sent ? [] : children);
          sent = true;
        },
      };
    },
  };
}

function droppedItems(entries: unknown[]) {
  return entries.map((entry) => ({ webkitGetAsEntry: () => entry }));
}

describe("WorkspaceView tree", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    serve(ROOT_WITH_SRC);
  });

  it("expands a folder in place instead of navigating away", async () => {
    const onPathChange = vi.fn();
    render(wrap(<WorkspaceView onPathChange={onPathChange} />));
    await waitFor(() => expect(screen.getByText("README.md")).toBeInTheDocument());

    await userEvent.click(screen.getByText("src"));

    // The child appears *below* its parent, both still on screen, and the route did
    // not change: that is what "expands in the same tree" means.
    expect(await screen.findByText("main.py")).toBeInTheDocument();
    expect(screen.getByText("src")).toBeInTheDocument();
    expect(screen.getByText("README.md")).toBeInTheDocument();
    expect(onPathChange).not.toHaveBeenCalled();
    expect(listSpy).toHaveBeenLastCalledWith("tok", `${ROOT}/src`, false, null);
  });

  it("collapses without refetching what it already has", async () => {
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());
    await userEvent.click(screen.getByText("src"));
    await screen.findByText("main.py");
    const callsAfterExpand = listSpy.mock.calls.length;

    await userEvent.click(screen.getByText("src"));
    await waitFor(() => expect(screen.queryByText("main.py")).not.toBeInTheDocument());
    await userEvent.click(screen.getByText("src"));

    expect(await screen.findByText("main.py")).toBeInTheDocument();
    // Re-expanding is free: the listing was kept.
    expect(listSpy.mock.calls.length).toBe(callsAfterExpand);
  });

  it("indents a child deeper than its parent", async () => {
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());
    await userEvent.click(screen.getByText("src"));
    await screen.findByText("main.py");

    const parentRow = screen.getByText("src").closest("button") as HTMLElement;
    const childRow = screen.getByText("main.py").closest("button") as HTMLElement;

    expect(parseInt(parentRow.style.paddingLeft, 10)).toBeLessThan(
      parseInt(childRow.style.paddingLeft, 10),
    );
  });

  it("opens a folder as the new root on double click", async () => {
    const onPathChange = vi.fn();
    render(wrap(<WorkspaceView onPathChange={onPathChange} />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());

    await userEvent.dblClick(screen.getByText("src"));

    expect(onPathChange).toHaveBeenCalledWith(`${ROOT}/src`);
  });
});

describe("WorkspaceView context menu", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    serve(ROOT_WITH_SRC);
  });

  it("offers file actions on a file and folder actions on a folder", async () => {
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("README.md")).toBeInTheDocument());

    fireEvent.contextMenu(screen.getByText("README.md"));
    const fileMenu = screen.getByRole("menu");
    expect(within(fileMenu).getByRole("menuitem", { name: "Preview" })).toBeInTheDocument();
    expect(within(fileMenu).getByRole("menuitem", { name: "Download" })).toBeInTheDocument();
    expect(within(fileMenu).getByRole("menuitem", { name: "Rename" })).toBeInTheDocument();
    expect(within(fileMenu).getByRole("menuitem", { name: "Delete" })).toBeInTheDocument();
    // A file is not a place to expand or to upload into.
    expect(within(fileMenu).queryByRole("menuitem", { name: "Expand" })).not.toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());

    fireEvent.contextMenu(screen.getByText("src"));
    const folderMenu = screen.getByRole("menu");
    expect(within(folderMenu).getByRole("menuitem", { name: "Expand" })).toBeInTheDocument();
    expect(within(folderMenu).getByRole("menuitem", { name: "Open as root" })).toBeInTheDocument();
    expect(within(folderMenu).getByRole("menuitem", { name: "Upload files here" })).toBeInTheDocument();
    expect(
      within(folderMenu).getByRole("menuitem", { name: "Upload folder here" }),
    ).toBeInTheDocument();
    expect(within(folderMenu).queryByRole("menuitem", { name: "Download" })).not.toBeInTheDocument();
  });

  it("renames from the menu, and the row becomes an input", async () => {
    vi.mocked(api.renameWorkspaceEntry).mockResolvedValue(
      listing(ROOT, [entry({ name: "GUIDE.md" })]),
    );
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("README.md")).toBeInTheDocument());

    fireEvent.contextMenu(screen.getByText("README.md"));
    await userEvent.click(screen.getByRole("menuitem", { name: "Rename" }));

    const input = screen.getByLabelText("Rename README.md");
    await userEvent.clear(input);
    await userEvent.type(input, "GUIDE.md");
    await userEvent.click(screen.getByRole("button", { name: "Confirm name" }));

    expect(api.renameWorkspaceEntry).toHaveBeenCalledWith(
      "tok",
      ROOT,
      "README.md",
      "GUIDE.md",
      false,
      null,
    );
  });

  it("creates a folder inside the folder it was asked on", async () => {
    vi.mocked(api.createWorkspaceFolder).mockResolvedValue(
      listing(`${ROOT}/src`, [entry({ name: "docs", kind: "directory", size: null })]),
    );
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());

    fireEvent.contextMenu(screen.getByText("src"));
    await userEvent.click(screen.getByRole("menuitem", { name: "New folder" }));
    await userEvent.type(screen.getByLabelText("New folder name"), "docs");
    await userEvent.click(screen.getByRole("button", { name: "Confirm name" }));

    // The parent is the folder the menu was opened on, not the root.
    expect(api.createWorkspaceFolder).toHaveBeenCalledWith("tok", `${ROOT}/src`, "docs", false, null);
  });

  it("closes on a click outside", async () => {
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("README.md")).toBeInTheDocument());
    fireEvent.contextMenu(screen.getByText("README.md"));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    fireEvent.pointerDown(document.body);

    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
  });

  it("offers nothing destructive for a link that leaves the workspace", async () => {
    serve({
      [ROOT]: listing(ROOT, [entry({ name: "escape", kind: "symlink", escapesWorkspace: true })]),
    });
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("escape")).toBeInTheDocument());

    fireEvent.contextMenu(screen.getByText("escape"));

    const menu = screen.getByRole("menu");
    expect(within(menu).queryByRole("menuitem", { name: "Delete" })).not.toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: "Rename" })).not.toBeInTheDocument();
    // Still a place to create or upload, because its *parent* is the directory.
    expect(within(menu).getByRole("menuitem", { name: "New folder" })).toBeInTheDocument();
  });
});

describe("WorkspaceView drag and drop", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    serve(ROOT_WITH_SRC);
  });

  it("moves an entry dropped onto a folder", async () => {
    vi.mocked(api.moveWorkspaceEntry).mockResolvedValue(
      listing(ROOT, [entry({ name: "src", kind: "directory", size: null })]),
    );
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("README.md")).toBeInTheDocument());

    const source = screen.getByText("README.md").closest("div[draggable]") as HTMLElement;
    const target = screen.getByText("src").closest("div[draggable]") as HTMLElement;
    const transfer = dataTransfer({ types: ["application/x-nanoinfra-workspace-entry"] });

    fireEvent.dragStart(source, { dataTransfer: transfer });
    fireEvent.dragOver(target, { dataTransfer: transfer });
    fireEvent.drop(target, { dataTransfer: transfer });

    await waitFor(() =>
      expect(api.moveWorkspaceEntry).toHaveBeenCalledWith(
        "tok",
        ROOT,
        "README.md",
        `${ROOT}/src`,
        false,
        null,
      ),
    );
  });

  it("refuses to drop a folder into its own subtree", async () => {
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());
    await userEvent.click(screen.getByText("src"));
    await screen.findByText("main.py");
    serve({
      ...ROOT_WITH_SRC,
      [`${ROOT}/src`]: listing(`${ROOT}/src`, [entry({ name: "inner", kind: "directory", size: null })]),
      [`${ROOT}/src/inner`]: listing(`${ROOT}/src/inner`, []),
    });

    const source = screen.getByText("src").closest("div[draggable]") as HTMLElement;
    const transfer = dataTransfer({ types: ["application/x-nanoinfra-workspace-entry"] });
    fireEvent.dragStart(source, { dataTransfer: transfer });
    // The dragged folder is its own destination.
    fireEvent.drop(source, { dataTransfer: transfer });

    expect(api.moveWorkspaceEntry).not.toHaveBeenCalled();
  });

  it("does not move an entry dropped back where it already is", async () => {
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("README.md")).toBeInTheDocument());
    await userEvent.click(screen.getByText("src"));
    await screen.findByText("main.py");

    const source = screen.getByText("README.md").closest("div[draggable]") as HTMLElement;
    const transfer = dataTransfer({ types: ["application/x-nanoinfra-workspace-entry"] });
    fireEvent.dragStart(source, { dataTransfer: transfer });
    fireEvent.drop(screen.getByTestId("workspace-tree"), { dataTransfer: transfer });

    expect(api.moveWorkspaceEntry).not.toHaveBeenCalled();
  });

  it("reviews OS files dropped onto a folder before writing them", async () => {
    uploadSpy.mockResolvedValue(listing(`${ROOT}/src`, [entry({ name: "dropped.txt" })]));
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());

    const target = screen.getByText("src").closest("div[draggable]") as HTMLElement;
    fireEvent.drop(target, {
      dataTransfer: dataTransfer({
        types: ["Files"],
        items: droppedItems([fileEntry("dropped.txt")]),
      }),
    });

    // Nothing is written until the operator says so, and the dialog names where.
    expect(await screen.findByRole("dialog")).toHaveTextContent("Upload to src");
    expect(uploadSpy).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: /Upload 1/ }));

    await waitFor(() =>
      expect(uploadSpy).toHaveBeenCalledWith(
        `${ROOT}/src`,
        "dropped.txt",
        expect.stringContaining("data:"),
        { includeHidden: false, relativePath: "dropped.txt", workspace: null },
      ),
    );
  });

  it("cancelling the review writes nothing", async () => {
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());
    fireEvent.drop(screen.getByTestId("workspace-tree"), {
      dataTransfer: dataTransfer({
        types: ["Files"],
        items: droppedItems([fileEntry("dropped.txt")]),
      }),
    });
    await screen.findByRole("dialog");

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(uploadSpy).not.toHaveBeenCalled();
  });

  it("says so when a drop turns out to hold no files", async () => {
    // An empty folder reported by the browser looks identical to a drop that never
    // fired, so it is said out loud instead of silently doing nothing.
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());

    fireEvent.drop(screen.getByTestId("workspace-tree"), {
      dataTransfer: dataTransfer({
        types: ["Files"],
        items: droppedItems([directoryEntry("empty", [])]),
      }),
    });

    expect(await screen.findByText(/nothing to upload/)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("uploads a dropped folder recursively, each file keeping its path", async () => {
    uploadSpy.mockResolvedValue(listing(ROOT, [entry({ name: "docs", kind: "directory", size: null })]));
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());

    // A dropped folder is not in `DataTransfer.files` at all: only the entry API
    // reaches inside it.
    fireEvent.drop(screen.getByTestId("workspace-tree"), {
      dataTransfer: dataTransfer({
        types: ["Files"],
        items: droppedItems([
          directoryEntry("docs", [
            fileEntry("index.md"),
            directoryEntry("img", [fileEntry("logo.png")]),
          ]),
        ]),
      }),
    });

    const dialog = await screen.findByRole("dialog");
    // The tree is shown, not just a count.
    expect(within(dialog).getByText("docs")).toBeInTheDocument();
    expect(within(dialog).getByText("img")).toBeInTheDocument();
    expect(within(dialog).getByText("logo.png")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Upload 2/ }));

    await waitFor(() => expect(uploadSpy).toHaveBeenCalledTimes(2));
    // The server creates the intermediate directories from these paths, so the client
    // sends no mkdir of its own.
    expect(uploadSpy.mock.calls.map((call) => call[3].relativePath)).toEqual([
      "docs/index.md",
      "docs/img/logo.png",
    ]);
    expect(uploadSpy.mock.calls.every((call) => call[0] === ROOT)).toBe(true);
  });

  it("leaves out what the operator removed from the review", async () => {
    uploadSpy.mockResolvedValue(listing(ROOT, [entry({ name: "docs", kind: "directory", size: null })]));
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());
    fireEvent.drop(screen.getByTestId("workspace-tree"), {
      dataTransfer: dataTransfer({
        types: ["Files"],
        items: droppedItems([
          directoryEntry("docs", [
            fileEntry("index.md"),
            directoryEntry("img", [fileEntry("logo.png"), fileEntry("icon.png")]),
          ]),
        ]),
      }),
    });
    await screen.findByRole("dialog");

    // Removing a folder removes everything under it.
    await userEvent.click(screen.getByRole("button", { name: "Remove img" }));
    await userEvent.click(screen.getByRole("button", { name: /Upload 1/ }));

    await waitFor(() => expect(uploadSpy).toHaveBeenCalledTimes(1));
    expect(uploadSpy.mock.calls[0][3].relativePath).toBe("docs/index.md");
  });

  it("sends a large file in frame-sized parts", async () => {
    // 20 MB in 8 MB parts: three frames, one upload id, and only the last one
    // answering with a listing.
    uploadSpy.mockImplementation(async (_parent, _name, _dataUrl, options) =>
      options.chunkIndex === options.chunkCount - 1
        ? listing(ROOT, [entry({ name: "big.bin" })])
        : null,
    );
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());
    fireEvent.drop(screen.getByTestId("workspace-tree"), {
      dataTransfer: dataTransfer({
        types: ["Files"],
        items: droppedItems([fileEntry("big.bin", 20 * 1024 * 1024)]),
      }),
    });
    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: /Upload 1/ }));

    await waitFor(() => expect(uploadSpy).toHaveBeenCalledTimes(3));
    const ids = new Set(uploadSpy.mock.calls.map((call) => call[3].uploadId));
    expect(ids.size).toBe(1);
    expect(uploadSpy.mock.calls.map((call) => call[3].chunkIndex)).toEqual([0, 1, 2]);
    expect(uploadSpy.mock.calls.every((call) => call[3].chunkCount === 3)).toBe(true);
  });

  it("refuses a file past the limit without sending it", async () => {
    // Chunking removed the frame size from what a person sees, so this is the real
    // cap now — and it is still checked before any bytes are read.
    uploadSpy.mockResolvedValue(listing(ROOT, [entry({ name: "small.txt" })]));
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());
    fireEvent.drop(screen.getByTestId("workspace-tree"), {
      dataTransfer: dataTransfer({
        types: ["Files"],
        items: droppedItems([
          directoryEntry("data", [
            fileEntry("huge.bin", 120 * 1024 * 1024),
            fileEntry("small.txt", 12),
          ]),
        ]),
      }),
    });
    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: /Upload 2/ }));

    // The small one still goes: one refusal does not abandon the rest.
    await waitFor(() => expect(uploadSpy).toHaveBeenCalledTimes(1));
    expect(uploadSpy.mock.calls[0][3].relativePath).toBe("data/small.txt");
    expect(await screen.findByText(/larger than the 100 MB limit/)).toBeInTheDocument();
  });

  it("leaves a dropped .git out of the plan, visibly", async () => {
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());

    fireEvent.drop(screen.getByTestId("workspace-tree"), {
      dataTransfer: dataTransfer({
        types: ["Files"],
        items: droppedItems([
          directoryEntry("project", [
            directoryEntry(".git", [fileEntry("HEAD"), fileEntry("config")]),
            fileEntry("main.py"),
          ]),
        ]),
      }),
    });

    const dialog = await screen.findByRole("dialog");
    // Shown and counted, not dropped from the plan: one click puts it back.
    expect(within(dialog).getByText(".git")).toBeInTheDocument();
    expect(within(dialog).getByText(/2 left out/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Upload 1$/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Include .git" })).toBeInTheDocument();
  });

  it("puts a removed folder back", async () => {
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());
    fireEvent.drop(screen.getByTestId("workspace-tree"), {
      dataTransfer: dataTransfer({
        types: ["Files"],
        items: droppedItems([
          directoryEntry("docs", [fileEntry("index.md"), directoryEntry("img", [fileEntry("logo.png")])]),
        ]),
      }),
    });
    await screen.findByRole("dialog");

    await userEvent.click(screen.getByRole("button", { name: "Remove img" }));
    expect(screen.getByRole("button", { name: /Upload 1/ })).toBeInTheDocument();

    // A mis-click on a folder holding half the upload must not mean dropping again.
    await userEvent.click(screen.getByRole("button", { name: "Include img" }));

    expect(screen.getByRole("button", { name: /Upload 2/ })).toBeInTheDocument();
  });

  it("reports the files a folder upload could not place, and keeps going", async () => {
    // A folder often holds something that already exists. Stopping at the first
    // refusal would leave the upload half done with no account of what landed.
    uploadSpy
      .mockRejectedValueOnce(new ApiError(409, "a file or folder with that name already exists"))
      .mockResolvedValueOnce(listing(ROOT, [entry({ name: "docs", kind: "directory", size: null })]));
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());

    fireEvent.drop(screen.getByTestId("workspace-tree"), {
      dataTransfer: dataTransfer({
        types: ["Files"],
        items: droppedItems([
          directoryEntry("docs", [fileEntry("index.md"), fileEntry("new.md")]),
        ]),
      }),
    });
    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: /Upload 2/ }));

    await waitFor(() => expect(uploadSpy).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/1 of 2 not uploaded/)).toBeInTheDocument();
    expect(screen.getByText(/docs\/index.md/)).toBeInTheDocument();
  });

  it("ignores a drag that is neither ours nor files", async () => {
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());

    const target = screen.getByText("src").closest("div[draggable]") as HTMLElement;
    // A text selection dragged in from elsewhere: acting on it would move a file
    // nobody picked up.
    fireEvent.drop(target, { dataTransfer: dataTransfer({ types: ["text/plain"] }) });

    expect(api.moveWorkspaceEntry).not.toHaveBeenCalled();
    expect(uploadSpy).not.toHaveBeenCalled();
  });
});

describe("WorkspaceView listing behaviour", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    serve(ROOT_WITH_SRC);
  });

  it("names the root and counts its entries", async () => {
    render(wrap(<WorkspaceView />));

    await waitFor(() => expect(screen.getByText("README.md")).toBeInTheDocument());
    expect(screen.getByText("w")).toBeInTheDocument();
    expect(screen.getByText("2 items")).toBeInTheDocument();
    expect(listSpy).toHaveBeenCalledWith("tok", null, false, null);
  });

  it("hides dot entries, says how many, and refetches when asked", async () => {
    serve({
      [ROOT]: listing(ROOT, [entry({ name: "notes.md" })], { hiddenCount: 2 }),
    });
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("notes.md")).toBeInTheDocument());
    expect(screen.getByText("1 item · 2 hidden")).toBeInTheDocument();

    listSpy.mockResolvedValue(
      listing(ROOT, [entry({ name: ".git", kind: "directory", size: null })], {
        includeHidden: true,
      }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Show dot files" }));

    // The server does the filtering, so revealing them is a refetch: a `.git` never
    // has to cross the wire to be hidden.
    await waitFor(() => expect(listSpy).toHaveBeenLastCalledWith("tok", null, true, null));
    expect(await screen.findByText(".git")).toBeInTheDocument();
  });

  it("shows a symlink that leaves the workspace and refuses to open it", async () => {
    serve({
      [ROOT]: listing(ROOT, [entry({ name: "escape", kind: "symlink", escapesWorkspace: true })]),
    });
    const onPathChange = vi.fn();
    render(wrap(<WorkspaceView onPathChange={onPathChange} />));
    await waitFor(() => expect(screen.getByText("escape")).toBeInTheDocument());

    const row = screen.getByText("escape").closest("button");
    expect(row).toBeDisabled();
    expect(row).toHaveAttribute(
      "title",
      "This link points outside the workspace and cannot be opened here",
    );
    expect(row?.closest("div[draggable]")).toHaveAttribute("draggable", "false");
    expect(onPathChange).not.toHaveBeenCalled();
  });

  it("reads a 403 as the containment boundary, not as a bug", async () => {
    listSpy.mockRejectedValue(new ApiError(403, "path is outside the current workspace"));
    render(wrap(<WorkspaceView path="/etc" />));

    await waitFor(() => expect(screen.getByText("Outside the workspace")).toBeInTheDocument());
  });

  it("relays what the gateway said instead of a bare status code", async () => {
    listSpy.mockRejectedValue(
      new ApiError(200, "Gateway returned WebUI HTML instead of JSON. Restart nanoinfra gateway and try again."),
    );
    render(wrap(<WorkspaceView />));

    await waitFor(() =>
      expect(screen.getByText(/Restart nanoinfra gateway and try again/)).toBeInTheDocument(),
    );
  });

  it("asks again with what the server said when a folder is not empty", async () => {
    vi.mocked(api.deleteWorkspaceEntry)
      .mockRejectedValueOnce(new ApiError(409, "the folder is not empty"))
      .mockResolvedValueOnce(listing(ROOT, []));
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Delete src" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete", exact: true }));

    await waitFor(() =>
      expect(screen.getByText("Delete “src” and everything in it?")).toBeInTheDocument(),
    );
    expect(api.deleteWorkspaceEntry).toHaveBeenLastCalledWith("tok", ROOT, "src", false, false, null);

    await userEvent.click(screen.getByRole("button", { name: "Delete everything" }));

    expect(api.deleteWorkspaceEntry).toHaveBeenLastCalledWith("tok", ROOT, "src", true, false, null);
  });

  it("offers the same drag-to-resize handle the thread's preview has", async () => {
    vi.mocked(api.fetchWorkspaceFilePreview).mockResolvedValue({
      path: `${ROOT}/README.md`,
      display_path: "README.md",
      project_path: ROOT,
      language: "markdown",
      content: "# hello",
      size: 7,
      truncated: false,
    });
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("README.md")).toBeInTheDocument());

    await userEvent.click(screen.getByText("README.md"));

    // The panel renders its handle only when a surface passes `onResizeStart`.
    expect(await screen.findByRole("button", { name: "Resize file preview" })).toBeInTheDocument();
  });

  it("collapses the tree in favour of the preview, and brings it back", async () => {
    vi.mocked(api.fetchWorkspaceFilePreview).mockResolvedValue({
      path: `${ROOT}/README.md`,
      display_path: "README.md",
      project_path: ROOT,
      language: "markdown",
      content: "# hello",
      size: 7,
      truncated: false,
    });
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("README.md")).toBeInTheDocument());
    // Nothing to collapse in favour of until a file is open.
    expect(screen.queryByRole("button", { name: "Collapse the tree" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByText("README.md"));
    await screen.findByTestId("file-preview-panel");
    await userEvent.click(screen.getByRole("button", { name: "Collapse the tree" }));

    // The tree is hidden, not unmounted: its expansions and cached listings survive.
    expect(screen.getByTestId("workspace-tree").closest("div.hidden")).not.toBeNull();
    // With nothing beside it, the panel is not describing a shared row any more.
    expect(screen.queryByRole("button", { name: "Resize file preview" })).not.toBeInTheDocument();

    await userEvent.click(screen.getAllByRole("button", { name: "Show the tree" })[0]);

    expect(screen.getByTestId("workspace-tree").closest("div.hidden")).toBeNull();
  });

  it("brings the tree back when the preview closes", async () => {
    // Otherwise closing into a collapsed tree leaves the surface empty.
    vi.mocked(api.fetchWorkspaceFilePreview).mockResolvedValue({
      path: `${ROOT}/README.md`,
      display_path: "README.md",
      project_path: ROOT,
      language: "markdown",
      content: "# hello",
      size: 7,
      truncated: false,
    });
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("README.md")).toBeInTheDocument());
    await userEvent.click(screen.getByText("README.md"));
    await screen.findByTestId("file-preview-panel");
    await userEvent.click(screen.getByRole("button", { name: "Collapse the tree" }));

    await userEvent.click(screen.getByRole("button", { name: "Close file preview" }));

    expect(screen.getByTestId("workspace-tree").closest("div.hidden")).toBeNull();
  });

  it("roots the tree where the preview's breadcrumb was clicked", async () => {
    serve({
      ...ROOT_WITH_SRC,
      [`${ROOT}/src`]: listing(`${ROOT}/src`, [entry({ name: "main.py" })]),
    });
    vi.mocked(api.fetchWorkspaceFilePreview).mockResolvedValue({
      path: `${ROOT}/src/main.py`,
      display_path: "src/main.py",
      project_path: ROOT,
      language: "python",
      content: "print()",
      size: 7,
      truncated: false,
    });
    const onPathChange = vi.fn();
    render(wrap(<WorkspaceView onPathChange={onPathChange} />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());
    await userEvent.click(screen.getByText("src"));
    await userEvent.click(await screen.findByText("main.py"));
    await screen.findByTestId("file-preview-panel");

    await userEvent.click(screen.getByRole("button", { name: "src" }));

    // The same move as "Open as root", asked for from the other pane.
    expect(onPathChange).toHaveBeenCalledWith(`${ROOT}/src`);
  });

  it("does not reload the open preview when something else re-renders the tree", async () => {
    // The approvals poll re-renders this view every 5 seconds. With the panel's
    // loader in its effect dependencies, each of those re-fetched the open file --
    // visible in the network log as one preview request per poll.
    vi.mocked(api.fetchWorkspaceFilePreview).mockResolvedValue({
      path: `${ROOT}/README.md`,
      display_path: "README.md",
      project_path: ROOT,
      language: "markdown",
      content: "# hello",
      size: 7,
      truncated: false,
    });
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("README.md")).toBeInTheDocument());
    await userEvent.click(screen.getByText("README.md"));
    await waitFor(() => expect(api.fetchWorkspaceFilePreview).toHaveBeenCalledTimes(1));

    // Any parent state change stands in for the poll: a drag-over sets `dropTarget`
    // and re-renders every row.
    const folder = screen.getByText("src").closest("div[draggable]") as HTMLElement;
    fireEvent.dragOver(folder, {
      dataTransfer: dataTransfer({ types: ["application/x-nanoinfra-workspace-entry"] }),
    });
    fireEvent.dragLeave(folder);
    await userEvent.click(screen.getByRole("button", { name: "Show dot files" }));

    expect(api.fetchWorkspaceFilePreview).toHaveBeenCalledTimes(1);
  });

  it("opens a file in the preview pane through the workspace-scoped route", async () => {
    vi.mocked(api.fetchWorkspaceFilePreview).mockResolvedValue({
      path: `${ROOT}/README.md`,
      display_path: "README.md",
      project_path: ROOT,
      language: "markdown",
      content: "# hello",
      size: 7,
      truncated: false,
    });
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("README.md")).toBeInTheDocument());

    await userEvent.click(screen.getByText("README.md"));

    await waitFor(() =>
      expect(api.fetchWorkspaceFilePreview).toHaveBeenCalledWith("tok", `${ROOT}/README.md`, null),
    );
  });
});
