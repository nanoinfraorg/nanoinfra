/**
 * The underlined tab strip, extracted from `AgentDetailFrame` the first time a second page needed
 * it — the Metrics destination (#235).
 *
 * Kept as its own component rather than copied, because a product with two tab strips that differ
 * by four pixels is a product where somebody has to decide which one is right every time a third
 * page appears. This is that decision, made once.
 *
 * **Not the segmented control with counts** that Automations uses for `All / Active / Paused`.
 * That one filters a single list and the count is the point; this one moves between surfaces that
 * have nothing to count. Two patterns, two jobs, and the difference is worth keeping.
 */
import { cn } from "@/lib/utils";

export interface TabStripItem<K extends string> {
  key: K;
  label: string;
}

export function TabStrip<K extends string>({
  items,
  value,
  onChange,
  ariaLabel,
  testId,
}: {
  items: ReadonlyArray<TabStripItem<K>>;
  value: K;
  onChange: (key: K) => void;
  ariaLabel: string;
  testId?: string;
}) {
  return (
    <div
      className="flex flex-wrap items-center gap-x-5 border-b border-border/50 px-1"
      role="tablist"
      aria-label={ariaLabel}
      data-testid={testId}
    >
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          role="tab"
          aria-selected={value === item.key}
          onClick={() => onChange(item.key)}
          className={cn(
            "-mb-px border-b-2 px-0.5 pb-2 pt-1 text-[13px] transition-colors",
            value === item.key
              ? "border-foreground font-medium text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground",
          )}
          data-testid={testId ? `${testId}-${item.key}` : undefined}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}
