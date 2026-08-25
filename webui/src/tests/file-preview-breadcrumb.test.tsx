import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FilePreviewPanel } from "@/components/FilePreviewPanel";
import { fetchFilePreview } from "@/lib/api";

vi.mock("@/components/CodeBlock", () => ({
  CodeBlock: () => <pre data-testid="mock-code-block" />,
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, fetchFilePreview: vi.fn() };
});

const WORKSPACE = "/home/dev/.nanoinfra/workspaces/default";
const FILE = `${WORKSPACE}/src/deep/notes.md`;

function payload() {
  return {
    path: FILE,
    display_path: "src/deep/notes.md",
    project_path: WORKSPACE,
    language: "markdown",
    content: "# hi",
    size: 4,
    truncated: false,
  };
}

describe("file preview breadcrumb", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.mocked(fetchFilePreview).mockResolvedValue(payload());
  });

  it("opens the directory a crumb names", async () => {
    const onNavigateToDirectory = vi.fn();
    render(
      <FilePreviewPanel
        path={FILE}
        token="tok"
        loadPreview={async () => payload()}
        onNavigateToDirectory={onNavigateToDirectory}
        onClose={() => {}}
      />,
    );
    await waitFor(() => expect(screen.getByTestId("mock-code-block")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "deep" }));

    expect(onNavigateToDirectory).toHaveBeenCalledWith(`${WORKSPACE}/src/deep`);
  });

  it("does not offer a crumb above the workspace root", async () => {
    // The listing routes refuse those paths, so offering them would be offering a
    // click that answers 403.
    const onNavigateToDirectory = vi.fn();
    render(
      <FilePreviewPanel
        path={`${WORKSPACE}/notes.md`}
        token="tok"
        loadPreview={async () => ({
          ...payload(),
          path: `${WORKSPACE}/notes.md`,
          display_path: "notes.md",
        })}
        onNavigateToDirectory={onNavigateToDirectory}
        onClose={() => {}}
      />,
    );
    await waitFor(() => expect(screen.getByTestId("mock-code-block")).toBeInTheDocument());

    // The compact crumbs here are `workspaces > default > notes.md`: only `default`
    // is the workspace itself, and `workspaces` sits above it.
    expect(screen.getByRole("button", { name: "default" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "workspaces" })).not.toBeInTheDocument();
  });

  it("never makes the file itself a button", async () => {
    render(
      <FilePreviewPanel
        path={FILE}
        token="tok"
        loadPreview={async () => payload()}
        onNavigateToDirectory={vi.fn()}
        onClose={() => {}}
      />,
    );
    await waitFor(() => expect(screen.getByTestId("mock-code-block")).toBeInTheDocument());

    expect(screen.queryByRole("button", { name: "notes.md" })).not.toBeInTheDocument();
    expect(screen.getByTestId("file-preview-title")).toHaveTextContent("notes.md");
  });

  it("stays plain text for a caller that cannot navigate", async () => {
    // The thread's own preview has no tree to move.
    render(
      <FilePreviewPanel sessionKey="websocket:chat" path={FILE} token="tok" onClose={() => {}} />,
    );
    await waitFor(() => expect(screen.getByTestId("mock-code-block")).toBeInTheDocument());

    expect(screen.queryByRole("button", { name: "deep" })).not.toBeInTheDocument();
  });
});
