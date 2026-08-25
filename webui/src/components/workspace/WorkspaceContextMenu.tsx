import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export interface ContextMenuItem {
  label: string;
  onSelect: () => void;
  icon?: ReactNode;
  danger?: boolean;
  /** Rendered as a separator above this item. */
  startsGroup?: boolean;
}

export interface ContextMenuAnchor {
  x: number;
  y: number;
}

/**
 * A cursor-anchored menu, hand-rolled rather than pulled in as
 * `@radix-ui/react-context-menu`.
 *
 * The dependency would be one more package for one surface, and this file is the
 * whole of what that surface needs: position, clamp, and close on the four things
 * that should close it. `TargetPicker` in the diagrams editor is the same choice.
 */
export function WorkspaceContextMenu({
  anchor,
  items,
  onClose,
}: {
  anchor: ContextMenuAnchor | null;
  items: ContextMenuItem[];
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [offset, setOffset] = useState({ x: 0, y: 0 });

  // Measured after paint, because the shift needed depends on the rendered size.
  // Opening near the right or bottom edge would otherwise put half the menu, or
  // the destructive item at the end of it, outside the window.
  useLayoutEffect(() => {
    const node = ref.current;
    if (!anchor || !node) {
      setOffset({ x: 0, y: 0 });
      return;
    }
    const box = node.getBoundingClientRect();
    const margin = 8;
    setOffset({
      x: box.right > window.innerWidth - margin ? window.innerWidth - margin - box.right : 0,
      y: box.bottom > window.innerHeight - margin ? window.innerHeight - margin - box.bottom : 0,
    });
  }, [anchor]);

  useEffect(() => {
    if (!anchor) return;
    const close = () => onClose();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    const onPointerDown = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) onClose();
    };
    // Capture on pointerdown so a click that lands on a row behind the menu closes
    // it instead of selecting that row as well.
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", close);
    window.addEventListener("blur", close);
    document.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", close);
      window.removeEventListener("blur", close);
      document.removeEventListener("scroll", close, true);
    };
  }, [anchor, onClose]);

  if (!anchor || items.length === 0) return null;

  return (
    <div
      ref={ref}
      role="menu"
      aria-label="Workspace actions"
      style={{ left: anchor.x + offset.x, top: anchor.y + offset.y }}
      className="fixed z-50 min-w-[11rem] overflow-hidden rounded-xl border border-border/70 bg-card/95 py-1 shadow-[0_18px_50px_rgba(15,23,42,0.22)] backdrop-blur-xl"
    >
      {items.map((item) => (
        <div key={item.label}>
          {item.startsGroup ? <div className="my-1 h-px bg-border/60" /> : null}
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              onClose();
              item.onSelect();
            }}
            className={cn(
              "flex w-full items-center gap-2.5 px-3 py-1.5 text-left text-[13px] transition-colors",
              item.danger
                ? "text-destructive-text hover:bg-destructive/10"
                : "text-foreground hover:bg-muted/70",
            )}
          >
            <span className="grid h-4 w-4 place-items-center text-muted-foreground">
              {item.icon}
            </span>
            {item.label}
          </button>
        </div>
      ))}
    </div>
  );
}
