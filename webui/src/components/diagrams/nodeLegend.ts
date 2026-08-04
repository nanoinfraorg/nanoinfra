import type { ComponentProvider } from "./componentCatalog";

export interface FieldLegend {
  legend: string[];
  legendTitle: string;
  hasMore: boolean;
}

// Never surface "secret" fields — a legend is a glanceable canvas summary,
// not a place to ever echo back sensitive values. Capped to maxFields so it
// stays a short caption instead of a full config dump that sprawls a wide
// node's whole width onto one line.
export function buildFieldLegend(
  provider: ComponentProvider | undefined,
  config: Record<string, string>,
  maxFields = 2,
): FieldLegend {
  const filled = provider?.fields.filter((field) => field.kind !== "secret" && config[field.key]) ?? [];
  const describe = (field: (typeof filled)[number]) => `${field.label}: ${config[field.key]}`;
  return {
    legend: filled.slice(0, maxFields).map(describe),
    legendTitle: filled.map(describe).join(" · "),
    hasMore: filled.length > maxFields,
  };
}
