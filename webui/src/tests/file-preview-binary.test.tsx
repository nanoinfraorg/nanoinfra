import { render, screen, waitFor } from "@testing-library/react";
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

function payload(over: Record<string, unknown>) {
  return {
    path: "/workspace/shot.png",
    display_path: "shot.png",
    project_path: "/workspace",
    language: "",
    content: "",
    size: 4096,
    truncated: false,
    ...over,
  };
}

describe("the preview panel opens a workspace file that is not text", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("shows an image instead of an empty code block", async () => {
    vi.mocked(fetchFilePreview).mockResolvedValue(payload({
      kind: "image",
      asset_url: "/api/workspace-asset/sig/payload",
    }) as never);

    render(<FilePreviewPanel sessionKey="websocket:chat" path="/workspace/shot.png" token="tok" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId("workspace-asset-img")).toBeInTheDocument());
    expect(screen.getByTestId("workspace-asset-img")).toHaveAttribute(
      "src", "/api/workspace-asset/sig/payload");
    expect(screen.queryByTestId("mock-code-block")).toBeNull();
    // Raw/Preview and the wrap toggle describe text, so they are not offered here.
    expect(screen.queryByTestId("file-preview-wrap")).toBeNull();
  });

  it("embeds a PDF in the panel rather than sending the reader elsewhere", async () => {
    vi.mocked(fetchFilePreview).mockResolvedValue(payload({
      display_path: "plan.pdf", kind: "pdf", asset_url: "/api/workspace-asset/s/p",
    }) as never);

    render(<FilePreviewPanel sessionKey="websocket:chat" path="/workspace/plan.pdf" token="tok" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId("workspace-asset-frame")).toBeInTheDocument());
    expect(screen.getByTestId("workspace-asset-frame")).toHaveAttribute("sandbox", "");
  });

  it("keeps rendering text exactly as before", async () => {
    vi.mocked(fetchFilePreview).mockResolvedValue(payload({
      display_path: "notes.txt", kind: "text", content: "hello", language: "text",
    }) as never);

    render(<FilePreviewPanel sessionKey="websocket:chat" path="/workspace/notes.txt" token="tok" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId("mock-code-block")).toBeInTheDocument());
    expect(screen.queryByTestId("workspace-asset-img")).toBeNull();
  });

  it("treats a gateway that sends no kind as text, which is all it could send", async () => {
    vi.mocked(fetchFilePreview).mockResolvedValue(payload({
      display_path: "notes.txt", content: "hello", language: "text",
    }) as never);

    render(<FilePreviewPanel sessionKey="websocket:chat" path="/workspace/notes.txt" token="tok" onClose={() => {}} />);

    await waitFor(() => expect(screen.getByTestId("mock-code-block")).toBeInTheDocument());
  });
});
