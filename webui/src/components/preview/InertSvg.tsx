import { useMemo } from "react";

import { toPortableSvg } from "@/lib/svg-xml";
import { cn } from "@/lib/utils";

/** Base64 of the UTF-8 bytes, chunked because a 384 KiB file is ~400k arguments to `apply`. */
function utf8ToBase64(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

/**
 * An SVG from the workspace, rendered as an image rather than as markup.
 *
 * The file is untrusted input -- the agent wrote it, or a `git pull` did, and diagrams here can
 * carry `secret://` references, so the contents are not assumed benign. An SVG loaded through
 * `<img>` is handled by the browser in secure static mode: scripting disabled and external
 * resources not fetched, which makes `<script>`, `on*` handlers, `<foreignObject>` and remote
 * `@import` / `<image href>` inert by construction rather than by a sanitiser someone has to keep
 * current. That last one is not hypothetical -- an SVG produced by mermaid.ink embeds an
 * `@import` of Font Awesome from a CDN, so a saved copy would phone out on view.
 *
 * The cost is that external fonts do not load, so a hand-written SVG naming a webfont falls back.
 */
export function InertSvg({
  markup,
  label,
  className,
}: {
  markup: string;
  label: string;
  className?: string;
}) {
  // An `<img>` parses its source as XML and needs an intrinsic size to lay out, and a `.svg` on
  // disk satisfies neither if it came from mermaid: unclosed `<br>` in a label, `width="100%"`
  // with no height. Normalised rather than refused, because the file is a perfectly good SVG for
  // every HTML consumer -- it is this stricter path that needs the repair.
  const src = useMemo(
    () => `data:image/svg+xml;base64,${utf8ToBase64(toPortableSvg(markup))}`,
    [markup],
  );

  return (
    <div className={cn("flex min-h-full items-center justify-center p-4", className)}>
      <img
        src={src}
        alt={label}
        className="max-h-full max-w-full"
        data-testid="inert-svg-preview"
      />
    </div>
  );
}
