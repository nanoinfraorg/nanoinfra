/**
 * What the editor should do about a diagram that changed server-side.
 *
 * - ``ignore``: the server holds exactly what is on screen. This is the common
 *   case, because the editor's *own* save comes back as a ``diagram_updated``
 *   frame like anyone else's write — reloading there would be a pointless
 *   re-render, and showing a banner would be a lie.
 * - ``apply``: the canvas has no unsaved edits, so it can follow the server
 *   silently. This is the "live" case.
 * - ``ask``: both sides moved. Nothing is overwritten without the operator.
 * - ``gone`` / ``gone-with-edits``: the diagram was deleted underneath us,
 *   with the second case saying there is still unsaved work to rescue.
 */
export type IncomingChangeVerdict = "ignore" | "apply" | "ask" | "gone" | "gone-with-edits";

export function resolveIncomingDiagramChange(args: {
  /** Fingerprint of what the server holds now, or ``null`` if it is gone. */
  fresh: string | null;
  /** Fingerprint of what the canvas currently shows. */
  onScreen: string;
  /** Fingerprint the server was last known to hold (load or successful save). */
  baseline: string;
}): IncomingChangeVerdict {
  const dirty = args.onScreen !== args.baseline;
  if (args.fresh === null) return dirty ? "gone-with-edits" : "gone";
  if (args.fresh === args.onScreen) return "ignore";
  return dirty ? "ask" : "apply";
}
