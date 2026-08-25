import { useCallback, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, FileText, Folder, RotateCcw, Upload, X } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  buildPlanTree,
  defaultExclusions,
  isExcluded,
  remainingFiles,
  toggleExclusion,
  totalBytes,
  type PlanNode,
} from "@/components/workspace/uploadPlan";
import type { DroppedFile } from "@/lib/dropped-files";
import { cn } from "@/lib/utils";

export interface UploadPlan {
  /** Absolute directory the tree lands in. */
  destination: string;
  /** How that directory is shown to a person. */
  destinationLabel: string;
  files: DroppedFile[];
  /** The collector hit its cap, so this is not everything that was dropped. */
  truncated: boolean;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

function PlanRow({
  node,
  depth,
  excluded,
  onToggle,
}: {
  node: PlanNode;
  depth: number;
  excluded: Set<string>;
  onToggle: (path: string) => void;
}) {
  // Folders start open: the point of this dialog is seeing what is about to be
  // written, and something collapsed by default is something unseen.
  const [open, setOpen] = useState(depth < 2);
  const off = isExcluded(node.path, excluded);
  const isDirectory = node.kind === "directory";

  return (
    <>
      <div
        className={cn(
          "group flex items-center gap-2 rounded-md py-1 pr-1 text-[13px]",
          off ? "opacity-45" : "hover:bg-muted/60",
        )}
        style={{ paddingLeft: `${depth * 14}px` }}
      >
        <button
          type="button"
          onClick={() => isDirectory && setOpen((previous) => !previous)}
          className="grid h-4 w-4 shrink-0 place-items-center text-muted-foreground"
          aria-label={isDirectory ? (open ? `Collapse ${node.name}` : `Expand ${node.name}`) : node.name}
          disabled={!isDirectory}
        >
          {isDirectory
            ? open
              ? <ChevronDown className="h-3.5 w-3.5" />
              : <ChevronRight className="h-3.5 w-3.5" />
            : null}
        </button>
        {isDirectory
          ? <Folder className="h-4 w-4 shrink-0 text-muted-foreground" />
          : <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />}
        <span className={cn("min-w-0 flex-1 truncate", off && "line-through")}>{node.name}</span>
        <span className="shrink-0 text-[11px] text-muted-foreground">
          {isDirectory ? `${node.count} file${node.count === 1 ? "" : "s"} · ` : ""}
          {formatSize(node.size)}
        </span>
        <button
          type="button"
          onClick={() => onToggle(node.path)}
          aria-label={off ? `Include ${node.name}` : `Remove ${node.name}`}
          title={off ? "Put it back" : "Leave it out of this upload"}
          className="rounded-full p-1 text-muted-foreground opacity-0 transition-opacity hover:bg-muted hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100 aria-[label^=Include]:opacity-100"
        >
          {off ? <RotateCcw className="h-3.5 w-3.5" /> : <X className="h-3.5 w-3.5" />}
        </button>
      </div>
      {isDirectory && open
        ? node.children.map((child) => (
          <PlanRow
            key={child.path}
            node={child}
            depth={depth + 1}
            excluded={excluded}
            onToggle={onToggle}
          />
        ))
        : null}
    </>
  );
}

/**
 * What is about to be written, before anything is.
 *
 * A folder upload is the one action here whose scope the operator cannot see from
 * the gesture that started it: dropping one folder can mean a thousand files. The
 * browser's own "upload N files to this site?" prompt counts them and says nothing
 * about what they are, so this lists the tree and lets anything be left out —
 * removal is reversible, because a mis-click on a folder holding half the upload
 * should not mean starting the drop again.
 */
export function WorkspaceUploadReview({
  plan,
  busy,
  progress,
  onCancel,
  onConfirm,
}: {
  plan: UploadPlan | null;
  busy: boolean;
  progress: { done: number; total: number; part?: { index: number; count: number } } | null;
  onCancel: () => void;
  onConfirm: (files: DroppedFile[]) => void;
}) {
  const tree = useMemo(() => buildPlanTree(plan?.files ?? []), [plan]);
  // Mounted per plan by the caller, so this initialiser runs once per proposal.
  const [excluded, setExcluded] = useState<Set<string>>(() => defaultExclusions(tree));
  const keeping = useMemo(
    () => remainingFiles(plan?.files ?? [], excluded),
    [excluded, plan],
  );
  const onToggle = useCallback(
    (path: string) => setExcluded((previous) => toggleExclusion(path, previous, tree)),
    [tree],
  );

  if (!plan) return null;

  const dropped = plan.files.length;
  const left = dropped - keeping.length;

  return (
    <Dialog open onOpenChange={(next) => (!next && !busy ? onCancel() : undefined)}>
      <DialogContent className="flex max-h-[80vh] w-[min(calc(100vw-2rem),34rem)] flex-col gap-0 rounded-[20px] p-5">
        <DialogHeader className="space-y-1">
          <DialogTitle className="text-[17px] font-semibold">Upload to {plan.destinationLabel}</DialogTitle>
          <DialogDescription className="text-[13px] text-muted-foreground">
            {`${keeping.length} of ${dropped} file${dropped === 1 ? "" : "s"} · ${formatSize(totalBytes(keeping))}`}
            {left > 0 ? ` · ${left} left out` : ""}
            {plan.truncated ? " · the drop held more than one upload carries" : ""}
          </DialogDescription>
        </DialogHeader>

        <div className="my-4 min-h-0 flex-1 overflow-y-auto rounded-xl border border-border/60 bg-muted/20 p-2">
          {tree.map((node) => (
            <PlanRow key={node.path} node={node} depth={0} excluded={excluded} onToggle={onToggle} />
          ))}
        </div>

        {progress ? (
          <div className="mb-3 text-[12px] text-muted-foreground">
            {progress.part
              ? `Uploading ${progress.done + 1}/${progress.total} · part ${progress.part.index}/${progress.part.count}…`
              : `Uploading ${progress.done}/${progress.total}…`}
            <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-muted">
              <div
                className="h-full bg-foreground/60 transition-[width]"
                style={{ width: `${Math.round((progress.done / Math.max(progress.total, 1)) * 100)}%` }}
              />
            </div>
          </div>
        ) : null}

        <DialogFooter className="!grid grid-cols-2 gap-3 space-x-0">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="h-10 w-full rounded-full bg-muted/70 text-[14px] font-semibold text-foreground hover:bg-muted disabled:opacity-60"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => onConfirm(keeping)}
            disabled={busy || keeping.length === 0}
            className="flex h-10 w-full items-center justify-center gap-2 rounded-full bg-foreground text-[14px] font-semibold text-background hover:bg-foreground/90 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <Upload className="h-4 w-4" />
            {busy ? "Uploading…" : `Upload ${keeping.length}`}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
