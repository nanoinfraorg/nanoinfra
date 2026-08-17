import { describe, expect, it } from "vitest";

import { toPortableSvg } from "@/lib/svg-xml";

function parsesAsXml(markup: string): boolean {
  const doc = new DOMParser().parseFromString(markup, "image/svg+xml");
  return doc.querySelector("parsererror") === null;
}

const MERMAID_SHAPED = [
  '<svg id="mermaid-1" width="100%" style="max-width: 1100px;" viewBox="0 0 1100 1120"',
  '     xmlns="http://www.w3.org/2000/svg">',
  '  <foreignObject width="120" height="40">',
  '    <div xmlns="http://www.w3.org/1999/xhtml">Cloudflare DNS<br>zone: barrahome.org</div>',
  "  </foreignObject>",
  "</svg>",
].join("\n");

describe("toPortableSvg", () => {
  it("closes the void tags that make mermaid's output invalid XML", () => {
    // The reported failure, exactly: Edge refused a downloaded diagram with
    // "mismatched tag. Expected: </br>".
    expect(parsesAsXml(MERMAID_SHAPED)).toBe(false);

    const portable = toPortableSvg(MERMAID_SHAPED);

    expect(parsesAsXml(portable)).toBe(true);
    expect(portable).not.toMatch(/<br>/);
  });

  it("keeps HTML labels in the XHTML namespace, so they still render", () => {
    // Asserted on the serialised string: the label's namespace is what makes a foreignObject
    // render as HTML rather than as unknown SVG elements, and it has to survive the round trip.
    const portable = toPortableSvg(MERMAID_SHAPED);

    expect(portable).toContain('xmlns="http://www.w3.org/1999/xhtml"');
    expect(portable).toContain("barrahome.org");
    // The regression this guards: an HTML round trip rewrote the label into the SVG namespace,
    // which parses and renders as nothing at all -- a diagram exported with no labels on it.
    expect(portable).not.toContain(`<div xmlns="${"http://www.w3.org/2000/svg"}"`);
  });

  it("gives a percentage-width diagram the intrinsic size an <img> needs", () => {
    // mermaid's default is width="100%" with no height, which resolves to a 0x0 image -- the
    // reason a PNG export came back empty rather than wrong.
    const portable = toPortableSvg(MERMAID_SHAPED);
    const svg = new DOMParser().parseFromString(portable, "image/svg+xml").documentElement;

    expect(svg.getAttribute("width")).toBe("1100");
    expect(svg.getAttribute("height")).toBe("1120");
  });

  it("stays fluid in the page after being given a size", () => {
    const svg = new DOMParser()
      .parseFromString(toPortableSvg(MERMAID_SHAPED), "image/svg+xml")
      .documentElement;
    const style = svg.getAttribute("style") ?? "";

    // mermaid's own cap is kept ahead of ours, and the later declaration is the one that applies.
    expect(style).toContain("max-width: 1100px");
    expect(style.lastIndexOf("max-width: 100%")).toBeGreaterThan(style.indexOf("max-width: 1100px"));
    expect(style).toContain("height: auto");
  });

  it("leaves a diagram that already has a real size alone", () => {
    const sized = '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200"'
      + ' viewBox="0 0 1100 1120"><rect width="10" height="10" /></svg>';
    const svg = new DOMParser()
      .parseFromString(toPortableSvg(sized), "image/svg+xml")
      .documentElement;

    expect(svg.getAttribute("width")).toBe("300");
    expect(svg.getAttribute("height")).toBe("200");
    expect(svg.getAttribute("style")).toBeNull();
  });

  it("invents no size when there is no viewBox to read one from", () => {
    const svg = new DOMParser()
      .parseFromString(toPortableSvg('<svg xmlns="http://www.w3.org/2000/svg" width="100%" />'), "image/svg+xml")
      .documentElement;

    expect(svg.getAttribute("width")).toBe("100%");
    expect(svg.getAttribute("height")).toBeNull();
  });

  it("adds the SVG namespace when a hand-written file omits it", () => {
    expect(toPortableSvg('<svg viewBox="0 0 10 10"><rect /></svg>')).toContain(
      'xmlns="http://www.w3.org/2000/svg"',
    );
  });

  it("returns markup with no svg element untouched", () => {
    expect(toPortableSvg("not markup at all")).toBe("not markup at all");
  });

  it("does not execute anything while normalising", () => {
    // The parse is inert by construction; asserted because that is what makes it safe to run
    // this over a file the agent wrote.
    const hostile = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
      + '<script>window.__svgXmlPwned = true;</script><rect /></svg>';

    toPortableSvg(hostile);

    expect((window as unknown as { __svgXmlPwned?: boolean }).__svgXmlPwned).toBeUndefined();
  });
});
