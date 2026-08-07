// Pastel border accent per catalog category (Edge, Compute, Data, ...) — a
// quick visual grouping cue at a glance across a busy diagram, never the
// only signal: the label/icon/provider text always identifies the exact
// component type on its own. Deliberately low-saturation/high-lightness so
// it reads as a soft tint on the dark theme, not a bold color-coded system —
// and there's no entry for "Layout" (Group boxes), which stay their existing
// neutral dashed border since they're a container, not a typed component.
const CATEGORY_BORDER_COLORS: Record<string, string> = {
  Edge: "hsl(211 55% 62% / 0.65)",
  Compute: "hsl(22 65% 58% / 0.65)",
  Applications: "hsl(262 48% 68% / 0.65)",
  Data: "hsl(150 45% 58% / 0.75)",
  Security: "hsl(340 55% 65% / 0.65)",
  Automation: "hsl(50 60% 58% / 0.65)",
  Observability: "hsl(188 50% 58% / 0.65)",
};

export function categoryBorderColor(category: string | undefined): string | undefined {
  return category ? CATEGORY_BORDER_COLORS[category] : undefined;
}
