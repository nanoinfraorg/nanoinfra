import { Download, FileQuestion } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ImageLightbox } from "@/components/ImageLightbox";
import { cn } from "@/lib/utils";

export type AssetKind = "image" | "pdf" | "binary";

interface WorkspaceAssetViewProps {
  kind: AssetKind;
  /** Signed `/api/workspace-asset/...` URL from the preview payload. */
  url: string | null;
  fileName: string;
  size: number;
}

function humanSize(bytes: number): string {
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

/**
 * The three things a workspace file can be when it is not text.
 *
 * The preview route used to answer 415 for all of them, so an image the agent had just written was
 * both unviewable and unlinkable — the availability probe rejected it before the path could become
 * a chip. Now the payload names its kind and carries a signed URL, and this decides the viewer:
 *
 * - an image renders inline, and clicking it opens the same `ImageLightbox` the thread uses, so
 *   there is one image viewer in the app rather than two;
 * - a PDF goes to the browser's own viewer in a sandboxed frame. The response carries
 *   `Content-Security-Policy: sandbox`, so script inside the document has no origin to act on;
 * - anything else is offered as a download, because the honest answer for a `.zip` is not a
 *   rendered preview — it is that the panel stops pretending the file is not there.
 */
export function WorkspaceAssetView({ kind, url, fileName, size }: WorkspaceAssetViewProps) {
  const { t } = useTranslation();
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);

  if (!url) {
    return (
      <div
        className="flex h-full items-center justify-center px-8 text-center text-sm text-muted-foreground"
        data-testid="workspace-asset-unavailable"
      >
        {t("filePreview.assetUnavailable", {
          defaultValue: "This file cannot be served from the current workspace.",
        })}
      </div>
    );
  }

  if (kind === "image") {
    return (
      <div className="flex h-full flex-col" data-testid="workspace-asset-image">
        <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto bg-muted/20 p-4">
          <button
            type="button"
            onClick={() => setLightboxIndex(0)}
            className="max-h-full cursor-zoom-in focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label={t("filePreview.openImage", { defaultValue: "Open image full size" })}
          >
            <img
              src={url}
              alt={fileName}
              className="max-h-full max-w-full object-contain"
              data-testid="workspace-asset-img"
            />
          </button>
        </div>
        <AssetFooter url={url} fileName={fileName} size={size} />
        <ImageLightbox
          images={[{ url, name: fileName }]}
          index={lightboxIndex}
          onIndexChange={setLightboxIndex}
          onOpenChange={(open) => setLightboxIndex(open ? 0 : null)}
        />
      </div>
    );
  }

  if (kind === "pdf") {
    return (
      <div className="flex h-full flex-col" data-testid="workspace-asset-pdf">
        <iframe
          src={url}
          title={fileName}
          // An empty sandbox: the browser's viewer renders the document, and script inside a PDF
          // gets no origin and no reach into this frame's parent.
          sandbox=""
          referrerPolicy="no-referrer"
          className="min-h-0 w-full flex-1 border-0 bg-muted/20"
          data-testid="workspace-asset-frame"
        />
        <AssetFooter url={url} fileName={fileName} size={size} />
      </div>
    );
  }

  return (
    <div
      className="flex h-full flex-col items-center justify-center gap-3 px-8 text-center"
      data-testid="workspace-asset-binary"
    >
      <FileQuestion className="h-6 w-6 text-muted-foreground/70" aria-hidden />
      <p className="text-sm text-foreground">{fileName}</p>
      <p className="text-xs text-muted-foreground">
        {t("filePreview.binaryNotRenderable", {
          defaultValue: "This file is not text, an image or a PDF, so there is nothing to render.",
        })}
        {" "}
        {humanSize(size)}
      </p>
      <DownloadLink url={url} fileName={fileName} />
    </div>
  );
}

function AssetFooter({ url, fileName, size }: { url: string; fileName: string; size: number }) {
  return (
    <div className="flex shrink-0 items-center justify-between gap-3 border-t border-border/70 px-4 py-2">
      <span className="truncate text-xs text-muted-foreground">{humanSize(size)}</span>
      <DownloadLink url={url} fileName={fileName} />
    </div>
  );
}

function DownloadLink({ url, fileName }: { url: string; fileName: string }) {
  const { t } = useTranslation();
  return (
    <a
      href={url}
      download={fileName}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs",
        "text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
      )}
      data-testid="workspace-asset-download"
    >
      <Download className="h-3.5 w-3.5" aria-hidden />
      {t("filePreview.download", { defaultValue: "Download" })}
    </a>
  );
}
