export function redactActivityText(value: string): string {
  return value
    .replace(/(https?:\/\/)[^/@\s]+@/gi, "$1<redacted>@")
    .replace(/\b(Bearer)\s+[A-Za-z0-9._~+/=-]+/gi, "$1 <redacted>")
    .replace(
      /(^|[\s;])((?:[A-Z0-9_]*)(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASS|AUTH)(?:[A-Z0-9_]*))=(?:"[^"]*"|'[^']*'|[^\s]+)/gim,
      "$1$2=<redacted>",
    )
    .replace(
      /(--(?:api-?key|access-?token|token|secret|password)(?:=|\s+))(?:"[^"]*"|'[^']*'|[^\s]+)/gi,
      "$1<redacted>",
    )
    .replace(/([?&](?:api_?key|access_?token|token|secret|password)=)[^&\s]+/gi, "$1<redacted>")
    .replace(
      /(["']?authorization["']?\s*[:=]\s*["']?)[^"'\r\n,;}]+/gi,
      "$1<redacted>",
    )
    .replace(
      /(["']?(?:api[_-]?key|access[_-]?token|token|secret|password)["']?\s*[:=]\s*)["']?[^"'\s,&;}]+["']?/gi,
      "$1<redacted>",
    )
    .replace(/\b(?:sk(?:-proj)?|xox[baprs]?|xapp)[-_][A-Za-z0-9._-]{8,}\b/gi, "<redacted>")
    .replace(/\bgh[pousr]_[A-Za-z0-9]{12,}\b/g, "<redacted>")
    .replace(/\bAKIA[A-Z0-9]{16}\b/g, "<redacted>")
    .replace(/\b\d{6,12}:[A-Za-z0-9_-]{20,}\b/g, "<redacted>");
}

export function redactShellCommand(command: string): string {
  return redactActivityText(command).replaceAll("<redacted>", "••••");
}

export function compactActivityPath(value: string): string {
  return value
    .replace(/\/Users\/[^/\s"']+/g, "~")
    .replace(/\/home\/[^/\s"']+/g, "~")
    .replace(/\/private\/tmp\/[^\s"']+/g, "/tmp/…")
    .replace(/\/var\/folders\/[^\s"']+/g, "/var/folders/…");
}

export function safeActivityDetail(value: string, maxLength = 96): string {
  return truncateMiddle(
    compactActivityPath(redactActivityText(value))
      .replace(/\/\.nanoinfra\/tool-results\/[^\s"']+/g, "/.nanoinfra/tool-results/…")
      .replace(/\s+/g, " ")
      .replace(/^["']|["']$/g, "")
      .trim(),
    maxLength,
  );
}

/**
 * The file a command writes, when the command says so plainly.
 *
 * Three sources, and deliberately only three: a `>`/`>>` redirect, an explicit `-o`/`--output`
 * flag, and an `open(..., "w")` in a heredoc body. Each of those *names* the destination.
 *
 * A positional output argument — `pdftotext in.pdf out.txt` — is **not** detected, because no
 * rule distinguishes it from a second input without knowing the program. Guessing there would
 * put a wrong filename in front of a reader, which is worse than putting none: the whole value
 * of this line is that it can be trusted.
 *
 * `/dev/null` and its siblings are excluded for the same reason they are excluded from the
 * workspace-bypass throttle — a redirect to the shell's own plumbing is not an artifact.
 */
export function shellCommandDestination(command: string): string | null {
  const text = command.replace(/\r\n/g, "\n");
  const candidates: string[] = [];

  // A redirect, but not `2>`/`&>` (those are streams, not results) and not an append to a log.
  for (const match of text.matchAll(/(^|[^0-9&>])>>?\s*("[^"]+"|'[^']+'|[^\s;|&<>]+)/g)) {
    candidates.push(match[2]);
  }
  for (const match of text.matchAll(/(?:^|\s)(?:-o|--output)(?:=|\s+)("[^"]+"|'[^']+'|[^\s;|&<>]+)/g)) {
    candidates.push(match[1]);
  }
  // `open(path, "w")` / `"wb"` / `"a"` inside a heredoc script.
  for (const match of text.matchAll(/open\(\s*("[^"]+"|'[^']+')\s*,\s*["'](?:w|wb|a|ab)["']/g)) {
    candidates.push(match[1]);
  }

  for (const raw of candidates) {
    const value = raw.replace(/^["']|["']$/g, "").trim();
    if (!value || value.startsWith("/dev/") || value === "-") continue;
    if (value.includes("$")) continue; // an unexpanded variable names nothing a reader can open
    return value;
  }
  return null;
}

export function summarizeShellCommand(command: string): string {
  const lines = redactShellCommand(command.replace(/\r\n/g, "\n"))
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const firstLine = compactActivityPath(lines[0] || "command");
  const firstPreview = truncateMiddle(firstLine, 92);
  return lines.length <= 1
    ? firstPreview
    : `${firstPreview} · script, ${lines.length} lines`;
}

function truncateMiddle(value: string, maxLength: number): string {
  if (value.length <= maxLength) return value;
  const head = Math.ceil((maxLength - 1) * 0.62);
  const tail = Math.floor((maxLength - 1) * 0.38);
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}
