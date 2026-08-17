import { beforeEach, describe, expect, it, vi } from "vitest";

import { mermaidConfigFor, mermaidDiagramPlugin, __resetMermaidForTests } from "@/lib/mermaid-plugin";

const mermaidModule = vi.hoisted(() => ({
  initialize: vi.fn(),
  render: vi.fn(async () => ({ svg: "<svg data-testid=\"chart\" />" })),
}));

vi.mock("mermaid", () => ({ default: mermaidModule }));

describe("the mermaid diagram plugin", () => {
  beforeEach(() => {
    mermaidModule.initialize.mockClear();
    mermaidModule.render.mockClear();
    __resetMermaidForTests();
  });

  it("is shaped the way Streamdown looks a diagram plugin up", () => {
    expect(mermaidDiagramPlugin.name).toBe("mermaid");
    expect(mermaidDiagramPlugin.type).toBe("diagram");
    // Streamdown routes a fence to this plugin by comparing the fence's language to it.
    expect(mermaidDiagramPlugin.language).toBe("mermaid");
  });

  it("initialises with the settings that make the injected SVG safe", async () => {
    // Streamdown renders the returned SVG with dangerouslySetInnerHTML, so mermaid's own
    // sanitiser is the boundary on this path. It is asserted, not assumed.
    await mermaidDiagramPlugin.getMermaid(mermaidConfigFor("light")).render("id-1", "flowchart TB");

    expect(mermaidModule.initialize).toHaveBeenCalledTimes(1);
    expect(mermaidModule.initialize.mock.calls[0]?.[0]).toMatchObject({
      securityLevel: "strict",
      startOnLoad: false,
    });
  });

  it("refuses to be talked out of strict mode by a caller's config", async () => {
    await mermaidDiagramPlugin
      .getMermaid({ ...mermaidConfigFor("light"), securityLevel: "loose" })
      .render("id-2", "flowchart TB");

    expect(mermaidModule.initialize.mock.calls[0]?.[0]).toMatchObject({
      securityLevel: "strict",
    });
  });

  it("loads and initialises mermaid once for repeated renders", async () => {
    const instance = mermaidDiagramPlugin.getMermaid(mermaidConfigFor("dark"));
    await instance.render("id-3", "flowchart TB");
    await instance.render("id-4", "flowchart LR");

    expect(mermaidModule.initialize).toHaveBeenCalledTimes(1);
    expect(mermaidModule.render).toHaveBeenCalledTimes(2);
  });

  it("re-initialises when the theme changes, so a dark preview is not a white box", async () => {
    await mermaidDiagramPlugin.getMermaid(mermaidConfigFor("light")).render("id-5", "flowchart TB");
    await mermaidDiagramPlugin.getMermaid(mermaidConfigFor("dark")).render("id-6", "flowchart TB");

    expect(mermaidModule.initialize).toHaveBeenCalledTimes(2);
    expect(mermaidModule.initialize.mock.calls[1]?.[0]).toMatchObject({ theme: "dark" });
  });

  it("passes the diagram source through untouched", async () => {
    const source = "flowchart TB\n    a[\"Web root dir<br/>/srv\"] --> b";
    await mermaidDiagramPlugin.getMermaid(mermaidConfigFor("light")).render("id-7", source);

    expect(mermaidModule.render).toHaveBeenCalledWith("id-7", source);
  });
});
