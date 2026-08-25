import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FilePreviewPanel } from "@/components/FilePreviewPanel";
import { fetchFilePreview } from "@/lib/api";
import { FILE_PREVIEW_WRAP_STORAGE_KEY } from "@/lib/local-preferences";

vi.mock("@/components/CodeBlock", () => ({
  CodeBlock: ({ wrapLongLines }: { wrapLongLines?: boolean }) => (
    <pre data-testid="mock-code-block" data-wrap={String(wrapLongLines)} />
  ),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, fetchFilePreview: vi.fn() };
});

const LONG = "a".repeat(400);

function payload() {
  return {
    path: "/workspace/notes.txt",
    display_path: "notes.txt",
    project_path: "/workspace",
    language: "text",
    content: LONG,
    size: LONG.length,
    truncated: false,
  };
}

describe("file preview wrapping", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.mocked(fetchFilePreview).mockResolvedValue(payload());
  });

  it("is off until asked for, and then remembered", async () => {
    const { unmount } = render(
      <FilePreviewPanel sessionKey="websocket:chat" path="/workspace/notes.txt" token="tok" onClose={() => {}} />,
    );
    await waitFor(() => expect(screen.getByTestId("mock-code-block")).toHaveAttribute("data-wrap", "false"));

    await userEvent.click(screen.getByTestId("file-preview-wrap"));

    expect(screen.getByTestId("mock-code-block")).toHaveAttribute("data-wrap", "true");
    // A reading habit, not a property of this file: it survives the panel.
    expect(window.localStorage.getItem(FILE_PREVIEW_WRAP_STORAGE_KEY)).toBe("1");
    unmount();

    render(
      <FilePreviewPanel sessionKey="websocket:chat" path="/workspace/notes.txt" token="tok" onClose={() => {}} />,
    );
    await waitFor(() => expect(screen.getByTestId("mock-code-block")).toHaveAttribute("data-wrap", "true"));
  });

  it("says which way the toggle goes", async () => {
    render(
      <FilePreviewPanel sessionKey="websocket:chat" path="/workspace/notes.txt" token="tok" onClose={() => {}} />,
    );
    await waitFor(() => expect(screen.getByTestId("file-preview-wrap")).toBeInTheDocument());

    expect(screen.getByRole("button", { name: "Wrap long lines" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    await userEvent.click(screen.getByTestId("file-preview-wrap"));
    expect(screen.getByRole("button", { name: "Stop wrapping long lines" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
});
