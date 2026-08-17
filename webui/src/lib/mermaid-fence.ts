const MIN_FENCE = 3;

/**
 * Wrap diagram source in a mermaid code fence that the source cannot escape.
 *
 * Streamdown's mermaid block is only reachable through a fence -- its renderer is not exported
 * from the package -- so previewing a `.mmd` file means handing the file to a markdown parser.
 * Markdown closes a fence at the first line whose backtick run is at least as long as the
 * opening delimiter, so a diagram whose comment contains ``` would end its own block and have
 * the remainder parsed as prose. The delimiter therefore grows past the longest run in the
 * content, which is the same rule as `fence_as_data` in `nanoinfra/utils/helpers.py`.
 */
export function mermaidFence(source: string): string {
  const content = source.replace(/\s+$/, "");
  const longestRun = Math.max(
    0,
    ...Array.from(content.matchAll(/`+/g), (match) => match[0].length),
  );
  const delimiter = "`".repeat(Math.max(MIN_FENCE, longestRun + 1));
  return `${delimiter}mermaid\n${content}\n${delimiter}`;
}
