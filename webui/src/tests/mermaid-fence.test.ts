import { describe, expect, it } from "vitest";

import { mermaidFence } from "@/lib/mermaid-fence";

describe("mermaidFence", () => {
  it("fences ordinary diagram source with three backticks", () => {
    expect(mermaidFence("flowchart TB\n    a --> b")).toBe(
      "```mermaid\nflowchart TB\n    a --> b\n```",
    );
  });

  it("outgrows a backtick run the file already contains", () => {
    // A markdown fence closes on the first line whose run is at least as long as the opening
    // one. A three-backtick fence around content that contains three backticks would end
    // early and hand the remainder of the diagram to the markdown parser as prose.
    const fenced = mermaidFence("flowchart TB\n%% ``` not the end\n    a --> b");

    expect(fenced.startsWith("````mermaid\n")).toBe(true);
    expect(fenced.endsWith("\n````")).toBe(true);
  });

  it("keeps outgrowing longer runs", () => {
    expect(mermaidFence("%% ````").startsWith("`````mermaid")).toBe(true);
    expect(mermaidFence("%% `````").startsWith("``````mermaid")).toBe(true);
  });

  it("leaves no line that could close the fence", () => {
    // The property, asserted on the built string rather than on rendered output: no line of
    // the content is a run at least as long as the delimiter.
    const content = "flowchart TB\n``` \n    a --> b\n`````";
    const fenced = mermaidFence(content);
    const delimiter = fenced.slice(0, fenced.indexOf("mermaid"));

    const closesEarly = content
      .split("\n")
      .some((line) => line.trimStart().startsWith(delimiter));

    expect(delimiter.length).toBeGreaterThan(5);
    expect(closesEarly).toBe(false);
  });

  it("does not leave a blank line inside the block", () => {
    expect(mermaidFence("flowchart TB\n\n\n")).toBe("```mermaid\nflowchart TB\n```");
  });

  it("still produces a valid block for an empty file", () => {
    expect(mermaidFence("")).toBe("```mermaid\n\n```");
  });
});
