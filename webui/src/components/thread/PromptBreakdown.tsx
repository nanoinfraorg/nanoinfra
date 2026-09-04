import { Fragment, useState } from "react";
import { ChevronRight } from "lucide-react";
import { useTranslation } from "react-i18next";

import { cn } from "@/lib/utils";
import type { PromptManifest } from "@/lib/types";

/**
 * Where a turn's input tokens went (#203).
 *
 * The question this exists for was asked about a real turn: *31K input tokens for a "hola"*. The
 * answer took an SSH session and a hand-written SQLite query — the system prompt was 7.4K and the
 * other 23K was tool schemas, three MCP servers carrying the same fifteen GitHub tools. None of
 * that was visible anywhere, which is the actual finding.
 *
 * Collapsed by default and grouped, because the useful reading is almost always one line: which
 * group is big. The per-section rows are for the turn after that.
 */
export function PromptBreakdown({ manifest }: { manifest: PromptManifest }) {
  const { t, i18n } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });
  const [open, setOpen] = useState(false);
  const numbers = new Intl.NumberFormat(i18n.language);

  const total = manifest.total_tokens;
  if (total <= 0) return null;

  // Largest first: the row worth reading is the one paying for the turn.
  const groups = Object.entries(manifest.groups).sort((a, b) => b[1] - a[1]);
  const sections = [...manifest.sections].sort((a, b) => b.tokens - a.tokens);

  return (
    <div className="mt-1 w-full">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className={cn(
          "inline-flex items-center gap-1 text-[11px] leading-4 text-muted-foreground/70",
          "transition-colors hover:text-foreground",
        )}
      >
        <ChevronRight
          className={cn("h-3 w-3 transition-transform", open && "rotate-90")}
          aria-hidden
        />
        {manifest.requests && manifest.requests > 1
          ? t("message.prompt.summaryFirstOf", {
              total: numbers.format(total),
              count: manifest.requests,
              defaultValue: "{{total}} in the first of {{count}} requests",
            })
          : tx("message.prompt.summary", "{{total}} in prompt", {
              total: numbers.format(total),
            })}
        <span className="text-muted-foreground/60">
          {groups
            .map(([name, tokens]) =>
              `${groupLabel(name, tx)} ${Math.round((tokens / total) * 100)}%`,
            )
            .join(" · ")}
        </span>
      </button>

      {open ? (
        <dl className="mt-1.5 grid grid-cols-[1fr_auto] gap-x-4 gap-y-0.5 text-[11px] leading-4">
          {sections.map((section) => (
            <PromptRow
              key={`${section.group}/${section.name}`}
              label={sourceLabel(section.name)}
              kind={sourceKind(section.name)}
              group={groupLabel(section.group, tx)}
              items={section.items}
              overridden={section.overridden}
              overriddenLabel={tx("message.prompt.replaced", "replaced")}
              tools={section.tools}
              tokens={section.tokens}
              share={section.tokens / total}
              format={(value) => numbers.format(value)}
              expandLabel={tx("message.prompt.whichTools", "which tools")}
            />
          ))}
          <dt className="pt-1 font-semibold text-foreground">
            {tx("message.prompt.total", "Total")}
          </dt>
          <dd className="pt-1 text-right font-semibold tabular-nums text-foreground">
            {numbers.format(total)}
          </dd>
          {/* Said once, at the bottom: the exact number is the provider's and it does not
              itemise, so every row above is this tokenizer's estimate. */}
          {manifest.peak_context_tokens ? (
            <dd className="col-span-2 pt-1 text-[10.5px] text-muted-foreground/70">
              {t("message.prompt.peak", {
                peak: numbers.format(manifest.peak_context_tokens),
                defaultValue: "The largest request of this turn reached {{peak}}",
              })}
            </dd>
          ) : null}
          {!manifest.measured ? (
            <dd className="col-span-2 pt-1 text-[10.5px] text-muted-foreground/70">
              {tx("message.prompt.estimated", "Estimated locally, per section")}
            </dd>
          ) : null}
        </dl>
      ) : null}
    </div>
  );
}

function PromptRow({
  label,
  kind,
  group,
  items,
  overridden,
  overriddenLabel,
  tools,
  tokens,
  share,
  format,
  expandLabel,
}: {
  label: string;
  /** `mcp` or `connector`, when the row is one server's or one connector's schemas. */
  kind?: string;
  group: string;
  items?: number;
  /** True when this deployment replaced the section rather than taking the platform's text. */
  overridden?: boolean;
  overriddenLabel: string;
  tools?: Array<{ name: string; chars: number; tokens: number }>;
  tokens: number;
  share: number;
  format: (value: number) => string;
  expandLabel: string;
}) {
  const [open, setOpen] = useState(false);
  // `×31` answers "how many" and invites "which ones?". The count itself opens the list, so the
  // answer is where the question is.
  const expandable = Boolean(tools?.length);

  return (
    <>
      <dt className="flex min-w-0 items-baseline gap-1.5 text-muted-foreground">
        <span className="truncate text-foreground">{label}</span>
        {kind ? (
          <span className="shrink-0 rounded-full bg-muted px-1.5 text-[10px] leading-4 text-muted-foreground/80">
            {kind}
          </span>
        ) : null}
        <span className="shrink-0 text-[10px] uppercase tracking-wide text-muted-foreground/60">
          {group}
        </span>
        {overridden ? (
          // Marked, not hidden. Without this a replaced section and the platform's own look
          // identical here -- same name, same group, a plausible size -- and the panel would be a
          // measurement that conceals the one difference a reader came for.
          <span
            className="shrink-0 rounded-full bg-primary/15 px-1.5 text-[10px] leading-4 text-primary"
            data-testid={`prompt-section-overridden-${label}`}
          >
            {overriddenLabel}
          </span>
        ) : null}
        {items ? (
          expandable ? (
            <button
              type="button"
              onClick={() => setOpen((value) => !value)}
              aria-expanded={open}
              // Named per row: two tool sections both offering "which tools" is a label that
              // identifies neither.
              aria-label={`${expandLabel}: ${label}`}
              className="shrink-0 text-muted-foreground/60 underline decoration-dotted decoration-from-font underline-offset-2 transition-colors hover:text-foreground"
            >
              ×{items}
            </button>
          ) : (
            <span className="shrink-0 text-muted-foreground/60">×{items}</span>
          )
        ) : null}
      </dt>
      <dd className="text-right tabular-nums text-muted-foreground">
        {format(tokens)}
        {/* A share rather than a bar: the reading is "which one is most of it", and 74% says
            that faster than a bar a reader has to compare against its neighbours. */}
        <span className="ml-2 text-muted-foreground/60">{Math.round(share * 100)}%</span>
      </dd>
      {open && tools
        ? tools.map((tool) => (
            <Fragment key={tool.name}>
              <dt className="truncate pl-3 font-mono text-[10.5px] leading-4 text-muted-foreground/80">
                {tool.name}
              </dt>
              <dd className="text-right font-mono text-[10.5px] leading-4 tabular-nums text-muted-foreground/70">
                {format(tool.tokens)}
              </dd>
            </Fragment>
          ))
        : null}
    </>
  );
}

/**
 * A tool row's name, read from its `Tool.source`.
 *
 * The raw source went straight into the row, so an operator saw `connector:google-calendar` where
 * the rest of the UI says "Google Calendar" -- and at the panel's size the colon was easy to miss,
 * which read as one run-together word. The prefix is a chip and the name is the name.
 */
function sourceLabel(name: string): string {
  const colon = name.indexOf(":");
  return colon > 0 ? name.slice(colon + 1) : name;
}

function sourceKind(name: string): string | undefined {
  const colon = name.indexOf(":");
  return colon > 0 ? name.slice(0, colon) : undefined;
}

function groupLabel(
  group: string,
  tx: (key: string, fallback: string) => string,
): string {
  if (group === "tools") return tx("message.prompt.groups.tools", "tools");
  if (group === "messages") return tx("message.prompt.groups.messages", "messages");
  if (group === "system") return tx("message.prompt.groups.system", "system");
  return group;
}
