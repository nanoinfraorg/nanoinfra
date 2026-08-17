const SVG_NS = "http://www.w3.org/2000/svg";

/** The HTML elements that need no closing tag, which is exactly what makes markup invalid XML. */
const VOID_TAGS = /<(area|base|br|col|embed|hr|img|input|link|meta|source|track|wbr)\b([^>]*?)\/?>/gi;

/**
 * Make SVG markup something a browser can load as an image and an XML tool can open.
 *
 * Two independent defects in what mermaid's `render` hands back, both of which only show up once
 * the string is consumed as `image/svg+xml` -- a data-URL `<img>`, a download opened in Edge, a
 * canvas rasterisation -- and neither of which affects it when injected into a page as HTML:
 *
 * - It is serialised from a live DOM, so HTML labels come back with `<br>` and `<img>` unclosed.
 *   Valid HTML, fatal XML: `mismatched tag. Expected: </br>`.
 * - Its root `<svg>` carries `width="100%"` and no height whenever `useMaxWidth` is on, which is
 *   mermaid's default (`calculateSvgSizeAttrs` in mermaid's core). A percentage width with no
 *   height means the image has **no intrinsic size**, so an `<img>` resolves to 0x0 and a canvas
 *   drawn from it yields an empty blob -- a PNG export that fails without failing.
 *
 * The void tags are closed textually and the result is then required to parse as XML before
 * anything else happens; if it does not, the original is returned untouched rather than a
 * half-repaired document. Deliberately *not* done by parsing as HTML and re-serialising, which is
 * the tempting version: an HTML parser is supposed to switch back to the HTML namespace inside
 * `<foreignObject>`, but when it does not, re-serialising rewrites mermaid's label `<div>` into
 * the SVG namespace and the diagram silently loses every label. Parsing as XML honours the
 * `xmlns` already in the markup instead of re-deriving it.
 *
 * XML parsing is inert -- no scripts run, no resources load -- so this is safe to run over a file
 * the agent wrote, and the result is still only ever handed to an `<img>`.
 */
export function toPortableSvg(markup: string): string {
  if (typeof DOMParser === "undefined" || typeof XMLSerializer === "undefined") return markup;

  const wellFormed = asWellFormedXml(markup);
  if (wellFormed === null) return markup;

  const root = parseXml(wellFormed)?.documentElement;
  if (!root || root.localName?.toLowerCase() !== "svg") return markup;

  if (!root.getAttribute("xmlns")) root.setAttribute("xmlns", SVG_NS);
  applyIntrinsicSize(root);

  return new XMLSerializer().serializeToString(root);
}

function asWellFormedXml(markup: string): string | null {
  if (parsesAsXml(markup)) return markup;
  const closed = markup.replace(VOID_TAGS, "<$1$2/>");
  return parsesAsXml(closed) ? closed : null;
}

function parseXml(markup: string): Document | null {
  try {
    const doc = new DOMParser().parseFromString(markup, "image/svg+xml");
    return doc.querySelector("parsererror") ? null : doc;
  } catch {
    return null;
  }
}

function parsesAsXml(markup: string): boolean {
  return parseXml(markup) !== null;
}

function applyIntrinsicSize(svg: Element): void {
  const width = (svg.getAttribute("width") ?? "").trim();
  const height = (svg.getAttribute("height") ?? "").trim();
  if (width !== "" && !width.endsWith("%") && height !== "") return;

  const viewBox = (svg.getAttribute("viewBox") ?? "").split(/[\s,]+/).filter(Boolean);
  if (viewBox.length !== 4) return;
  const [, , boxWidth, boxHeight] = viewBox;
  if (!isPositiveNumber(boxWidth) || !isPositiveNumber(boxHeight)) return;

  svg.setAttribute("width", boxWidth as string);
  svg.setAttribute("height", boxHeight as string);

  // Appended, not replaced: mermaid's own `max-width: NNNpx` stays in the attribute and the later
  // declaration wins, so the diagram still shrinks to its container instead of overflowing it.
  const style = svg.getAttribute("style") ?? "";
  const prefix = style.trim() === "" ? "" : `${style.replace(/;\s*$/, "")}; `;
  svg.setAttribute("style", `${prefix}max-width: 100%; height: auto;`);
}

function isPositiveNumber(value: string | undefined): boolean {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0;
}
