import { describe, expect, it } from "vitest";
import { shellCommandDestination } from "@/components/thread/activity/activity-text";

describe("shellCommandDestination", () => {
  it("reads a redirect", () => {
    expect(shellCommandDestination("pdftotext -layout in.pdf > out.txt")).toBe("out.txt");
    expect(shellCommandDestination("echo hi >> notes.md")).toBe("notes.md");
  });
  it("reads an output flag", () => {
    expect(shellCommandDestination("soffice --output deck.pdf x.pptx")).toBe("deck.pdf");
    expect(shellCommandDestination("tool -o report.xlsx")).toBe("report.xlsx");
    expect(shellCommandDestination("tool --output=report.xlsx")).toBe("report.xlsx");
  });
  it("reads an open() in a heredoc body", () => {
    expect(shellCommandDestination("python3 - <<'EOF'\nwith open('cfe.txt', 'w') as f:\n  f.write(x)\nEOF")).toBe("cfe.txt");
  });
  it("names nothing for the shell's plumbing", () => {
    expect(shellCommandDestination("ls skills 2>/dev/null")).toBeNull();
    expect(shellCommandDestination("cmd > /dev/null 2>&1")).toBeNull();
  });
  it("names nothing for a positional output it cannot identify", () => {
    expect(shellCommandDestination("pdftotext -layout in.pdf out.txt")).toBeNull();
  });
  it("names nothing for an unexpanded variable", () => {
    expect(shellCommandDestination("cmd > $OUT")).toBeNull();
  });
  it("names nothing when there is no destination", () => {
    expect(shellCommandDestination("python3 -c 'import openpyxl'")).toBeNull();
    expect(shellCommandDestination("ls -la")).toBeNull();
  });
});
