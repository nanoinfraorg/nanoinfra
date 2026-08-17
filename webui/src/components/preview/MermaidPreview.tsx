import { useMemo } from "react";
import { AlertCircle } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Streamdown, type MermaidErrorComponentProps } from "streamdown";
import "streamdown/styles.css";

import { useThemeValue } from "@/hooks/useTheme";
import { mermaidFence } from "@/lib/mermaid-fence";
import { mermaidConfigFor, mermaidDiagramPlugin } from "@/lib/mermaid-plugin";
import { cn } from "@/lib/utils";

/**
 * A `.mmd` file rendered as a diagram.
 *
 * Streamdown already ships the mermaid block -- pan/zoom, fullscreen, copy, and download as
 * mmd/svg/png -- but its renderer is not exported from the package, so the only way in is a
 * fence. Hence `mermaidFence`, which also stops a file whose comment contains ``` from closing
 * its own block. This renders Streamdown directly instead of threading new props through
 * `MarkdownText`, so the chat renderer is untouched by the preview panel.
 */
export function MermaidPreview({
  source,
  onShowSource,
  className,
}: {
  source: string;
  onShowSource?: () => void;
  className?: string;
}) {
  const { t } = useTranslation();
  const theme = useThemeValue();

  const config = useMemo(() => mermaidConfigFor(theme), [theme]);
  const fenced = useMemo(() => mermaidFence(source), [source]);

  const errorComponent = useMemo(
    () =>
      function MermaidRenderError({ error }: MermaidErrorComponentProps) {
        return (
          <div
            className="flex flex-col items-start gap-2 px-4 py-6 text-sm text-muted-foreground"
            data-testid="mermaid-preview-error"
          >
            <p className="flex items-start gap-2">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" aria-hidden />
              <span>
                {t("filePreview.mermaidFailed", {
                  defaultValue: "This diagram could not be rendered.",
                })}
              </span>
            </p>
            <pre className="max-w-full overflow-x-auto whitespace-pre-wrap text-xs text-muted-foreground/85">
              {error}
            </pre>
            {onShowSource ? (
              <button
                type="button"
                onClick={onShowSource}
                className="rounded-md border border-border/70 px-2 py-1 text-xs font-medium text-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {t("filePreview.viewSource", { defaultValue: "View source" })}
              </button>
            ) : null}
          </div>
        );
      },
    [onShowSource, t],
  );

  return (
    <Streamdown
      mode="static"
      parseIncompleteMarkdown={false}
      plugins={{ mermaid: mermaidDiagramPlugin }}
      mermaid={{ config, errorComponent }}
      controls={{ mermaid: { copy: true, download: true, fullscreen: true, panZoom: true } }}
      className={cn("min-h-full px-3 py-2", className)}
    >
      {fenced}
    </Streamdown>
  );
}
