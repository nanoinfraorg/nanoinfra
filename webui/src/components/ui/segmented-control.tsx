import { cn } from "@/lib/utils";

export interface SegmentedControlOption {
  value: string;
  label: string;
}

/** The pill-shaped two-or-more-way switch used across Settings and the file preview header.
 *
 * Two `aria-pressed` buttons in a `role="group"` rather than a checkbox, so a screen reader
 * announces which representation is active instead of an on/off state.
 */
export function SegmentedControl({
  value,
  options,
  onChange,
  ariaLabel,
  disabled = false,
  title,
  size = "md",
  className,
}: {
  value: string;
  options: SegmentedControlOption[];
  onChange: (value: string) => void;
  ariaLabel?: string;
  disabled?: boolean;
  title?: string;
  size?: "sm" | "md";
  className?: string;
}) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      title={title}
      className={cn(
        "inline-flex items-center rounded-full bg-muted p-0.5 font-medium text-muted-foreground",
        size === "sm" ? "h-7 text-[11px]" : "h-8 text-[12px]",
        disabled && "opacity-50",
        className,
      )}
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={value === option.value}
          disabled={disabled}
          onClick={() => onChange(option.value)}
          className={cn(
            "rounded-full transition-colors",
            size === "sm" ? "px-2 py-0.5" : "px-3 py-1",
            disabled ? "cursor-not-allowed" : null,
            value === option.value
              && "bg-background text-foreground ring-1 ring-inset ring-border/45",
          )}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
