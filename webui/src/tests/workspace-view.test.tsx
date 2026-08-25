import { render, screen, waitFor } from "@testing-library/react";
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
  };
});

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

function entry(overrides: Partial<WorkspaceEntry> & { name: string }): WorkspaceEntry {
  return {
    kind: "file",
    size: 12,
    modified: "2026-08-19T10:00:00+00:00",
    escapesWorkspace: false,
    ...overrides,
  };
}

function listing(overrides: Partial<WorkspaceListingPayload> = {}): WorkspaceListingPayload {
  return {
    path: "/home/dev/project",
    displayPath: "",
    projectPath: "/home/dev/project",
    parent: null,
    entries: [
      entry({ name: "src", kind: "directory", size: null }),
      entry({ name: "README.md" }),
    ],
    truncated: false,
    ...overrides,
  };
}

const listSpy = vi.mocked(api.fetchWorkspaceListing);

describe("WorkspaceView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listSpy.mockResolvedValue(listing());
  });

  it("lists the workspace root and names it in the breadcrumb", async () => {
    render(wrap(<WorkspaceView />));

    await waitFor(() => expect(screen.getByText("README.md")).toBeInTheDocument());
    expect(listSpy).toHaveBeenCalledWith("tok", null);
    expect(screen.getByRole("button", { name: "project" })).toBeInTheDocument();
    expect(screen.getByText("2 items")).toBeInTheDocument();
  });

  it("navigates into a directory with the absolute path the server answered", async () => {
    const onPathChange = vi.fn();
    render(wrap(<WorkspaceView onPathChange={onPathChange} />));
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());

    await userEvent.click(screen.getByText("src"));

    expect(onPathChange).toHaveBeenCalledWith("/home/dev/project/src");
  });

  it("offers a step up only when the server said there is one", async () => {
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("README.md")).toBeInTheDocument());
    // At the root the payload's `parent` is null, so there is no step to offer.
    expect(screen.queryByRole("button", { name: /up/i })).not.toBeInTheDocument();

    listSpy.mockResolvedValue(
      listing({
        path: "/home/dev/project/src",
        displayPath: "src",
        parent: "/home/dev/project",
        entries: [entry({ name: "main.py" })],
      }),
    );
    const onPathChange = vi.fn();
    render(wrap(<WorkspaceView path="/home/dev/project/src" onPathChange={onPathChange} />));
    await waitFor(() => expect(screen.getByText("main.py")).toBeInTheDocument());

    await userEvent.click(screen.getAllByRole("button", { name: /up/i })[0]);
    expect(onPathChange).toHaveBeenCalledWith("/home/dev/project");
  });

  it("shows a symlink that leaves the workspace, and refuses to open it", async () => {
    // Listed rather than hidden: a name that silently vanishes is more confusing
    // than one that says why it cannot be followed. The server refuses it too.
    listSpy.mockResolvedValue(
      listing({
        entries: [entry({ name: "escape", kind: "symlink", escapesWorkspace: true })],
      }),
    );
    const onPathChange = vi.fn();
    render(wrap(<WorkspaceView onPathChange={onPathChange} />));
    await waitFor(() => expect(screen.getByText("escape")).toBeInTheDocument());

    const row = screen.getByText("escape").closest("button");
    expect(row).toBeDisabled();
    expect(row).toHaveAttribute(
      "title",
      "This link points outside the workspace and cannot be opened here",
    );
    expect(onPathChange).not.toHaveBeenCalled();
  });

  it("reads a 403 as the containment boundary, not as a bug", async () => {
    listSpy.mockRejectedValue(new ApiError(403, "path is outside the current workspace"));
    render(wrap(<WorkspaceView path="/etc" />));

    await waitFor(() => expect(screen.getByText("Outside the workspace")).toBeInTheDocument());
  });

  it("says when a directory held more than one listing carries", async () => {
    listSpy.mockResolvedValue(listing({ truncated: true }));
    render(wrap(<WorkspaceView />));

    await waitFor(() =>
      expect(screen.getByText(/list cut, directory holds more/)).toBeInTheDocument(),
    );
  });

  it("opens a file in the preview pane through the workspace-scoped route", async () => {
    vi.mocked(api.fetchWorkspaceFilePreview).mockResolvedValue({
      path: "/home/dev/project/README.md",
      display_path: "README.md",
      project_path: "/home/dev/project",
      language: "markdown",
      content: "# hello",
      size: 7,
      truncated: false,
    });
    render(wrap(<WorkspaceView />));
    await waitFor(() => expect(screen.getByText("README.md")).toBeInTheDocument());

    await userEvent.click(screen.getByText("README.md"));

    await waitFor(() =>
      expect(api.fetchWorkspaceFilePreview).toHaveBeenCalledWith(
        "tok",
        "/home/dev/project/README.md",
      ),
    );
  });
});
