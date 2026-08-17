import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { FilePreviewPanel } from "@/components/FilePreviewPanel";
import { setAppLanguage } from "@/i18n";
import { fetchFilePreview } from "@/lib/api";
import { FILE_PREVIEW_MODES_STORAGE_KEY } from "@/lib/local-preferences";

vi.mock("@/components/CodeBlock", () => ({
  CodeBlock: ({ code, language }: { code: string; language?: string }) => (
    <pre data-testid="mock-code-block" data-language={language}>{code}</pre>
  ),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, fetchFilePreview: vi.fn() };
});

const DIAGRAM = "flowchart TB\n    client[\"Client (browser)\"] --> dns[\"Cloudflare DNS\"]";

function payload(overrides: Partial<{
  display_path: string;
  language: string;
  content: string;
  truncated: boolean;
}>) {
  return {
    path: `/workspace/${overrides.display_path ?? "topology.mmd"}`,
    display_path: overrides.display_path ?? "topology.mmd",
    project_path: "/workspace",
    language: overrides.language ?? "mermaid",
    content: overrides.content ?? DIAGRAM,
    size: (overrides.content ?? DIAGRAM).length,
    truncated: overrides.truncated ?? false,
  };
}

function openPanel(path: string) {
  return render(
    <FilePreviewPanel
      sessionKey="websocket:chat-1"
      path={path}
      token="tok"
      onClose={() => {}}
    />,
  );
}

const toggle = () => screen.queryByRole("group", { name: "How to show this file" });

/** Streamdown defers a diagram until it scrolls into view, and jsdom has no observer at all,
 * so without this the block mounts and then waits forever. */
class ImmediateIntersectionObserver {
  constructor(private readonly callback: IntersectionObserverCallback) {}
  observe(target: Element): void {
    this.callback(
      [{ isIntersecting: true, target } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    );
  }
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

describe("the file preview render toggle", () => {
  beforeEach(async () => {
    await setAppLanguage("en");
    window.localStorage.clear();
    vi.stubGlobal("IntersectionObserver", ImmediateIntersectionObserver);
    vi.mocked(fetchFilePreview).mockReset();
  });

  it("opens a diagram rendered, and shows its source on request", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchFilePreview).mockResolvedValue(payload({}));

    openPanel("diagrams/topology.mmd");

    // The mermaid block is Streamdown's; only its renderer is lazy, so the container is the
    // honest assertion that the fence reached the diagram path rather than the code path.
    await waitFor(() => {
      expect(document.querySelector('[data-streamdown="mermaid-block"]')).not.toBeNull();
    });
    expect(screen.queryByTestId("mock-code-block")).toBeNull();
    // Streamdown's own container for the rendered chart. Under jsdom it stays empty -- there is
    // no text measurement, so mermaid returns no SVG -- but reaching it proves the fence was
    // routed to the diagram path and not to a code block, and that nothing failed on the way.
    // Re-queried rather than held: Streamdown replaces this node when the render resolves, so a
    // reference captured by `findBy` is already detached by the time it is asserted. Generous
    // timeout because reaching it means mermaid was imported and parsed -- ~600 kB of async
    // chunk, which the 1s default clears alone and misses under a loaded worker pool.
    await waitFor(
      () => {
        expect(screen.queryByLabelText("Mermaid chart")).not.toBeNull();
      },
      { timeout: 15000 },
    );
    expect(screen.queryByTestId("mermaid-preview-error")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Raw" }));

    expect(await screen.findByTestId("mock-code-block")).toHaveTextContent("flowchart TB");
    expect(document.querySelector('[data-streamdown="mermaid-block"]')).toBeNull();
  }, 30000);

  it("offers no toggle for a file with no visual form", async () => {
    vi.mocked(fetchFilePreview).mockResolvedValue(
      payload({ display_path: "quicksort.py", language: "python", content: "print('ok')" }),
    );

    openPanel("quicksort.py");

    await screen.findByTestId("mock-code-block");
    expect(toggle()).toBeNull();
  });

  it("renders an SVG as an image, so nothing in the file can execute", async () => {
    const hostile = [
      "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"10\" height=\"10\">",
      "  <style>@import url(\"https://cdnjs.cloudflare.com/font-awesome.css\");</style>",
      "  <script>window.__pwned = true;</script>",
      "  <rect width=\"10\" height=\"10\" />",
      "</svg>",
    ].join("\n");
    vi.mocked(fetchFilePreview).mockResolvedValue(
      payload({ display_path: "barrahome.svg", language: "svg", content: hostile }),
    );

    const { container } = openPanel("diagrams/barrahome.svg");

    const image = await screen.findByTestId("inert-svg-preview");
    expect(image.getAttribute("src")).toMatch(/^data:image\/svg\+xml;base64,/);
    // The point of the data-URL <img>: the markup is never parsed into this document, so the
    // script and the remote @import are inert without a sanitiser in the path.
    expect(container.querySelector("script")).toBeNull();
    // `rect` and `style` come only from the file. (Not `svg` -- the breadcrumb chevron is one.)
    expect(container.querySelector("rect")).toBeNull();
    expect(container.querySelector("style")).toBeNull();
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined();
  });

  it("keeps a truncated file as source, because half a diagram is not a smaller diagram", async () => {
    vi.mocked(fetchFilePreview).mockResolvedValue(payload({ truncated: true }));

    openPanel("diagrams/topology.mmd");

    await screen.findByTestId("mock-code-block");
    expect(document.querySelector('[data-streamdown="mermaid-block"]')).toBeNull();
    expect(screen.getByRole("button", { name: "Preview" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Raw" })).toBeDisabled();
  });

  it("remembers the choice for the next file of the same kind", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchFilePreview).mockResolvedValue(payload({}));

    const first = openPanel("diagrams/topology.mmd");
    await waitFor(() => {
      expect(document.querySelector('[data-streamdown="mermaid-block"]')).not.toBeNull();
    });
    await user.click(screen.getByRole("button", { name: "Raw" }));
    await screen.findByTestId("mock-code-block");
    first.unmount();

    vi.mocked(fetchFilePreview).mockResolvedValue(payload({ display_path: "other.mmd" }));
    openPanel("diagrams/other.mmd");

    expect(await screen.findByTestId("mock-code-block")).toBeInTheDocument();
    expect(document.querySelector('[data-streamdown="mermaid-block"]')).toBeNull();
  });

  it("falls back to the default when the stored preference is corrupt", async () => {
    window.localStorage.setItem(FILE_PREVIEW_MODES_STORAGE_KEY, "{\"mmd\":\"sideways\"}");
    vi.mocked(fetchFilePreview).mockResolvedValue(payload({}));

    openPanel("diagrams/topology.mmd");

    await waitFor(() => {
      expect(document.querySelector('[data-streamdown="mermaid-block"]')).not.toBeNull();
    });
  });

  it("opens markdown as source, with the toggle available", async () => {
    vi.mocked(fetchFilePreview).mockResolvedValue(
      payload({ display_path: "notes.md", language: "markdown", content: "# Notes" }),
    );

    openPanel("notes.md");

    expect(await screen.findByTestId("mock-code-block")).toHaveTextContent("# Notes");
    expect(toggle()).not.toBeNull();
    expect(screen.getByRole("button", { name: "Raw" })).toHaveAttribute("aria-pressed", "true");
  });

  it("shows a parse failure in place of the diagram, with a way back to the source", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchFilePreview).mockResolvedValue(
      payload({ content: "flowchart TB\n    a --> " }),
    );

    openPanel("diagrams/broken.mmd");

    // Real mermaid, real parse failure: the message names the problem, which is the whole
    // point of showing it rather than an empty box.
    const failure = await screen.findByTestId("mermaid-preview-error", {}, { timeout: 15000 });
    expect(failure).toHaveTextContent("could not be rendered");

    await user.click(screen.getByRole("button", { name: "View source" }));
    expect(await screen.findByTestId("mock-code-block")).toHaveTextContent("flowchart TB");
  }, 30000);
});
