import { Handle, Position } from "@xyflow/react";

const HANDLE_CLASS = "!h-2 !w-2 !border-none !bg-border";

/**
 * Every node gets both a source and a target handle stacked at each of the
 * 4 sides (8 handles, 4 visible dots) -- with only one role per side, a
 * connection could only ever start at bottom/right and only ever land on
 * top/left (React Flow's default `connectionMode="strict"` only allows a
 * `type="source"` handle to connect to a `type="target"` one).
 *
 * `connectionMode="loose"` on `<ReactFlow>` is necessary but not sufficient
 * by itself: even in loose mode, `getEdgePosition`'s handle lookup always
 * resolves an edge's *source* end from that node's source-type bucket only
 * (`sourceHandleBounds.source`) -- so dropping a connection onto a
 * side that has only a target-type handle there still fails to render
 * ("Couldn't create edge for source handle id"), because whichever node
 * ends up normalized as the edge's `source` needs a handle with that exact
 * id in its *source* bucket specifically. Giving every side both roles,
 * under the same id, means that id always exists in whichever bucket a
 * given connection ends up needing.
 *
 * Every id here (`top`/`bottom`/`left`/`right`) is explicit on purpose:
 * @xyflow/system's handle lookup falls back to "the first handle of that
 * type" for any handle whose id is falsy (`!handleId ? bounds[0] : ...`),
 * so a handle left at its default (no id) can silently resolve to a
 * *different* handle that happens to be declared earlier in the same
 * type bucket.
 */
export function ConnectionHandles() {
  return (
    <>
      <Handle type="target" position={Position.Top} id="top" className={HANDLE_CLASS} />
      <Handle type="source" position={Position.Top} id="top" className={HANDLE_CLASS} />
      <Handle type="target" position={Position.Left} id="left" className={HANDLE_CLASS} />
      <Handle type="source" position={Position.Left} id="left" className={HANDLE_CLASS} />
      <Handle type="source" position={Position.Right} id="right" className={HANDLE_CLASS} />
      <Handle type="target" position={Position.Right} id="right" className={HANDLE_CLASS} />
      <Handle type="source" position={Position.Bottom} id="bottom" className={HANDLE_CLASS} />
      <Handle type="target" position={Position.Bottom} id="bottom" className={HANDLE_CLASS} />
    </>
  );
}
