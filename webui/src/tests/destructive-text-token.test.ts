import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * The destructive colour decision, pinned in source.
 *
 * ``--destructive`` is a surface colour. It pairs with ``--destructive-foreground``
 * for text drawn on that surface. A destructive button keeps that pair, and the
 * button holds its contrast.
 *
 * Body text on a ``bg-destructive/10`` panel needs a separate colour. The dark
 * surface value scores 1.30:1 as body text, so an operator cannot read it. Body
 * text uses ``text-destructive-text`` instead. That token clears WCAG AA on both
 * themes.
 *
 * This test refuses a bare ``text-destructive`` class under ``src/components``.
 * It keeps a later edit from a quiet return to the surface token as body text.
 */

/**
 * Finds the webui root on disk.
 *
 * The test environment serves modules over http, so ``import.meta.url`` is not a
 * file path here. The walk starts at the working directory instead.
 */
function webuiRoot(): string {
  let dir = process.cwd();
  for (let step = 0; step < 6; step += 1) {
    if (existsSync(join(dir, "tailwind.config.js")) && existsSync(join(dir, "src", "components"))) {
      return dir;
    }
    const nested = join(dir, "webui");
    if (existsSync(join(nested, "tailwind.config.js"))) return nested;
    dir = dirname(dir);
  }
  throw new Error("The webui root is not reachable from the working directory.");
}

const WEBUI_ROOT = webuiRoot();
const COMPONENTS_DIR = join(WEBUI_ROOT, "src", "components");
const GLOBALS_CSS = join(WEBUI_ROOT, "src", "globals.css");
const TAILWIND_CONFIG = join(WEBUI_ROOT, "tailwind.config.js");

/**
 * Files that may keep a bare ``text-destructive`` class.
 *
 * Each entry is a path relative to ``src/components``, with forward slashes. Give
 * each new entry a comment that states why the class is a surface colour there.
 * The list is empty today, because every red label now reads the text token.
 */
const SURFACE_EXCEPTIONS = new Set<string>([]);

/** Matches ``text-destructive`` but skips the foreground pair and the text token. */
const BARE_TEXT_DESTRUCTIVE = /\btext-destructive(?!-foreground\b)(?!-text\b)/;

/** Matches the opacity modifier on the text token, such as ``/80``. */
const TEXT_TOKEN_OPACITY = /\btext-destructive-text\/(\d+)\b/g;

/**
 * The Tailwind opacity scale, in percent.
 *
 * Tailwind emits no rule for a value outside this scale, so the colour then
 * disappears without a warning. A value such as ``/78`` is dead CSS.
 */
const OPACITY_SCALE = new Set(
  Array.from({ length: 21 }, (_, step) => step * 5),
);

/** Lists every TypeScript source file under one directory tree. */
function sourceFiles(dir: string, found: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) sourceFiles(full, found);
    else if (/\.tsx?$/.test(entry.name)) found.push(full);
  }
  return found;
}

/** Reads one CSS rule, from its selector to the matched close brace. */
function cssBlock(css: string, selector: string): string {
  const start = css.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`globals.css has no ${selector} block.`);
  let depth = 0;
  for (let index = css.indexOf("{", start); index < css.length; index += 1) {
    if (css[index] === "{") depth += 1;
    if (css[index] === "}") {
      depth -= 1;
      if (depth === 0) return css.slice(start, index + 1);
    }
  }
  throw new Error(`The ${selector} block has no close brace.`);
}

/** Reads one custom property value out of a CSS rule. */
function tokenValue(block: string, token: string): string {
  const match = new RegExp(`${token}:\\s*([^;]+);`).exec(block);
  if (!match) throw new Error(`The block has no ${token} declaration.`);
  return match[1].trim();
}

describe("destructive text token", () => {
  it("declares a separate text token in both themes", () => {
    const css = readFileSync(GLOBALS_CSS, "utf8");

    for (const selector of [":root", ".dark"]) {
      const block = cssBlock(css, selector);
      const surface = tokenValue(block, "--destructive");
      const text = tokenValue(block, "--destructive-text");
      // A text token that repeats the surface value brings the bug back.
      expect(text).not.toBe(surface);
    }
  });

  it("maps the text token in the Tailwind colour theme", () => {
    const config = readFileSync(TAILWIND_CONFIG, "utf8");
    expect(config).toContain('text: "hsl(var(--destructive-text))"');
    expect(config).toContain('foreground: "hsl(var(--destructive-foreground))"');
  });

  it("uses no bare text-destructive class under src/components", () => {
    const offences: string[] = [];

    for (const file of sourceFiles(COMPONENTS_DIR)) {
      const path = relative(COMPONENTS_DIR, file).split(sep).join("/");
      if (SURFACE_EXCEPTIONS.has(path)) continue;
      const lines = readFileSync(file, "utf8").split("\n");
      lines.forEach((line, index) => {
        if (!BARE_TEXT_DESTRUCTIVE.test(line)) return;
        offences.push(`${path}:${index + 1}: ${line.trim()}`);
      });
    }

    // Use text-destructive-text for a red label. Keep text-destructive-foreground
    // for text on a filled destructive surface, such as a destructive button.
    expect(offences).toEqual([]);
  });

  it("keeps every text token opacity on the Tailwind scale", () => {
    const offences: string[] = [];

    for (const file of sourceFiles(COMPONENTS_DIR)) {
      const path = relative(COMPONENTS_DIR, file).split(sep).join("/");
      const lines = readFileSync(file, "utf8").split("\n");
      lines.forEach((line, index) => {
        for (const match of line.matchAll(TEXT_TOKEN_OPACITY)) {
          if (OPACITY_SCALE.has(Number(match[1]))) continue;
          offences.push(`${path}:${index + 1}: ${match[0]}`);
        }
      });
    }

    // An off-scale value gives no CSS, so the red label loses its colour.
    expect(offences).toEqual([]);
  });
});
