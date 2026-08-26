import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { WorkspaceAssetView } from "@/components/preview/WorkspaceAssetView";

const URL_ = "/api/workspace-asset/sig/payload";

describe("a workspace file that is not text", () => {
  it("renders an image inline, from the signed URL", () => {
    render(<WorkspaceAssetView kind="image" url={URL_} fileName="shot.png" size={2048} />);

    const img = screen.getByTestId("workspace-asset-img");
    expect(img).toHaveAttribute("src", URL_);
    expect(img).toHaveAttribute("alt", "shot.png");
    // Downloadable as well: a picture you can see is not always a picture you have.
    expect(screen.getByTestId("workspace-asset-download")).toHaveAttribute("href", URL_);
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
  });

  it("gives a PDF to the browser's viewer in a sandboxed frame", () => {
    render(<WorkspaceAssetView kind="pdf" url={URL_} fileName="plan.pdf" size={1024 * 1024} />);

    const frame = screen.getByTestId("workspace-asset-frame");
    expect(frame).toHaveAttribute("src", URL_);
    // Empty sandbox: script inside the document gets no origin and cannot reach the parent.
    expect(frame).toHaveAttribute("sandbox", "");
    expect(frame).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(screen.getByText("1.0 MB")).toBeInTheDocument();
  });

  it("offers a download for anything else, rather than pretending the file is not there", () => {
    render(<WorkspaceAssetView kind="binary" url={URL_} fileName="bundle.zip" size={500} />);

    expect(screen.getByTestId("workspace-asset-binary")).toBeInTheDocument();
    expect(screen.getByText("bundle.zip")).toBeInTheDocument();
    const link = screen.getByTestId("workspace-asset-download");
    expect(link).toHaveAttribute("href", URL_);
    expect(link).toHaveAttribute("download", "bundle.zip");
    expect(screen.queryByTestId("workspace-asset-img")).toBeNull();
  });

  it("says so when the gateway could not sign a URL for it", () => {
    render(<WorkspaceAssetView kind="image" url={null} fileName="shot.png" size={10} />);

    expect(screen.getByTestId("workspace-asset-unavailable")).toBeInTheDocument();
    expect(screen.queryByTestId("workspace-asset-img")).toBeNull();
  });
});
