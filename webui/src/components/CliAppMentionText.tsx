import { useMemo } from "react";
import { useTranslation } from "react-i18next";

import {
  INLINE_TOKEN_HIGHLIGHT_COLOR,
  InlineTokenHighlight,
} from "@/components/InlineTokenHighlight";
import { useLogoFallback } from "@/hooks/useLogoFallback";
import { logoFallbackUrls } from "@/lib/provider-brand";
import type { CliAppInfo, McpPresetInfo, SessionMention } from "@/lib/types";
import { cn } from "@/lib/utils";

type CliAppMentionSegment =
  | { kind: "text"; text: string }
  | { kind: "cli"; text: string; app: CliAppInfo };

/** A resource mention as it appears in text: the id is the token, the name is what is shown. */
export interface ResourceMentionTarget {
  kind: "server" | "diagram";
  id: string;
  name: string;
  detail: string;
}

export type CapabilityMentionSegment =
  | CliAppMentionSegment
  | { kind: "mcp"; text: string; preset: McpPresetInfo }
  | { kind: "session"; text: string; mention: SessionMention }
  | { kind: "resource"; text: string; resource: ResourceMentionTarget };

export function cliAppInitials(app: CliAppInfo): string {
  const value = app.display_name || app.name;
  return (
    value
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0]?.toUpperCase())
      .join("") || app.name.slice(0, 2).toUpperCase()
  );
}
export function mcpPresetInitials(preset: Pick<McpPresetInfo, "name" | "display_name">): string {
  const value = preset.display_name || preset.name;
  return (
    value
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0]?.toUpperCase())
      .join("") || preset.name.slice(0, 2).toUpperCase()
  );
}
export function splitCapabilityMentionSegments(
  value: string,
  cliApps: CliAppInfo[],
  mcpPresets: McpPresetInfo[] = [],
  sessionMentions: SessionMention[] = [],
  resources: ResourceMentionTarget[] = [],
): CapabilityMentionSegment[] {
  if (
    !value
    || (
      cliApps.length === 0
      && mcpPresets.length === 0
      && sessionMentions.length === 0
      && resources.length === 0
    )
  ) {
    return value ? [{ kind: "text", text: value }] : [];
  }
  const cliAppsByName = new Map(
    cliApps
      .filter((app) => app.installed)
      .map((app) => [app.name.toLowerCase(), app]),
  );
  const mcpPresetsByName = new Map(
    mcpPresets
      .filter((preset) => preset.installed && preset.configured)
      .map((preset) => [preset.name.toLowerCase(), preset]),
  );
  const sessionsByName = new Map(
    sessionMentions.map((mention) => [mention.name.toLowerCase(), mention]),
  );
  // Both forms, because the text carries the name when it is tokenisable and the id otherwise.
  const resourcesByToken = new Map<string, ResourceMentionTarget>();
  for (const resource of resources) {
    resourcesByToken.set(`${resource.kind}:${resource.id}`.toLowerCase(), resource);
    resourcesByToken.set(`${resource.kind}:${resource.name}`.toLowerCase(), resource);
  }
  if (
    cliAppsByName.size === 0
    && mcpPresetsByName.size === 0
    && sessionsByName.size === 0
    && resourcesByToken.size === 0
  ) {
    return [{ kind: "text", text: value }];
  }

  const segments: CapabilityMentionSegment[] = [];
  // Two patterns rather than one, because a resource token carries `kind:id` and a name token
  // cannot contain a colon. An id this list does not know stays plain text, so a reference to
  // something deleted reads as text instead of rendering as a chip that resolves to nothing.
  // Two shapes. A resource token is `kind:` followed by a name or an id, and a real name holds
  // dots and other punctuation -- "barrahome.org" -- so it runs to the next separator rather than
  // to a narrow character class. A bare name token keeps the class it always had.
  // Greedy, not lazy: a lazy match stopped at the first dot and resolved "server:barrahome",
  // which is a different thing or nothing at all. Trailing sentence punctuation the greedy match
  // swallows is trimmed back below until the token resolves.
  const mentionRe = /(^|[\s([{])@((?:server|diagram):[^\s([{)\]}]+|[\p{L}\p{N}_-]+)(?=$|[^\p{L}\p{N}_:.-])/giu;
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = mentionRe.exec(value)) !== null) {
    const prefix = match[1] ?? "";
    let name = match[2] ?? "";
    let resource = resourcesByToken.get(name.toLowerCase());
    // "@server:barrahome.org." ending a sentence: trim what the greedy match took rather than
    // failing to resolve a reference the operator plainly made.
    while (!resource && /[.,;!?]$/.test(name)) {
      name = name.slice(0, -1);
      resource = resourcesByToken.get(name.toLowerCase());
    }
    const key = name.toLowerCase();
    const app = resource ? null : cliAppsByName.get(key);
    const preset = resource || app ? null : mcpPresetsByName.get(key);
    const session = resource || app || preset ? null : sessionsByName.get(key);
    if (!resource && !app && !preset && !session) continue;

    const mentionStart = match.index + prefix.length;
    const mentionEnd = mentionStart + name.length + 1;
    if (mentionStart > cursor) {
      segments.push({ kind: "text", text: value.slice(cursor, mentionStart) });
    }
    if (resource) {
      segments.push({
        kind: "resource",
        text: value.slice(mentionStart, mentionEnd),
        resource,
      });
    } else if (app) {
      segments.push({ kind: "cli", text: value.slice(mentionStart, mentionEnd), app });
    } else if (preset) {
      segments.push({ kind: "mcp", text: value.slice(mentionStart, mentionEnd), preset });
    } else if (session) {
      segments.push({
        kind: "session",
        text: value.slice(mentionStart, mentionEnd),
        mention: session,
      });
    }
    cursor = mentionEnd;
  }
  if (cursor < value.length) {
    segments.push({ kind: "text", text: value.slice(cursor) });
  }
  return segments.length ? segments : [{ kind: "text", text: value }];
}

export function CliAppMentionText({
  text,
  cliApps,
  mcpPresets = [],
  sessionMentions = [],
}: {
  text: string;
  cliApps: CliAppInfo[];
  mcpPresets?: McpPresetInfo[];
  sessionMentions?: SessionMention[];
}) {
  const segments = splitCapabilityMentionSegments(text, cliApps, mcpPresets, sessionMentions);
  if (!segments.some((segment) => segment.kind !== "text")) return <>{text}</>;
  return (
    <>
      {segments.map((segment, index) => {
        if (segment.kind === "text") {
          return <span key={`text-${index}`}>{segment.text}</span>;
        }
        return (
          <CapabilityMentionToken
            key={`${segment.kind}-${index}`}
            segment={segment}
            variant="message"
          />
        );
      })}
    </>
  );
}

export function CapabilityMentionToken({
  segment,
  variant,
  isHero = false,
}: {
  segment: Exclude<CapabilityMentionSegment, { kind: "text" }>;
  variant: "composer" | "message";
  isHero?: boolean;
}) {
  if (segment.kind === "cli") {
    return (
      <CliAppMentionToken
        app={segment.app}
        label={segment.text}
        variant={variant}
        isHero={isHero}
      />
    );
  }
  if (segment.kind === "mcp") {
    return (
      <McpPresetMentionToken
        preset={segment.preset}
        label={segment.text}
        variant={variant}
        isHero={isHero}
      />
    );
  }
  if (segment.kind === "resource") {
    return (
      <ResourceMentionToken
        resource={segment.resource}
        label={segment.text}
        variant={variant}
      />
    );
  }
  return <SessionMentionToken mention={segment.mention} label={segment.text} variant={variant} />;
}

/**
 * Renders the source text, not a shortened label.
 *
 * The composer draws this as an overlay behind a transparent textarea, so a decoration narrower
 * than the text it covers drags the caret out of position and opens a gap. Every other mention
 * token does the same for the same reason.
 */
export function ResourceMentionToken({
  resource,
  label,
  variant,
}: {
  resource: ResourceMentionTarget;
  label: string;
  variant: "composer" | "message";
}) {
  const testIdPrefix = variant === "composer" ? "composer" : "message";
  const kindLabel = resource.kind === "server" ? "Server" : "Diagram";
  return (
    <InlineTokenHighlight
      testId={`${testIdPrefix}-resource-mention-${resource.kind}-${resource.id}`}
      title={
        resource.detail
          ? `${kindLabel}: ${resource.name} · ${resource.detail}`
          : `${kindLabel}: ${resource.name}`
      }
      color={INLINE_TOKEN_HIGHLIGHT_COLOR}
    >
      {label}
    </InlineTokenHighlight>
  );
}

export function SessionMentionToken({
  mention,
  label,
  variant,
}: {
  mention: SessionMention;
  label: string;
  variant: "composer" | "message";
}) {
  const testIdPrefix = variant === "composer" ? "composer" : "message";
  const token = (
    <InlineTokenHighlight
      testId={`${testIdPrefix}-session-mention-${mention.name}`}
      title={`Session: ${mention.title || mention.name}`}
      color={INLINE_TOKEN_HIGHLIGHT_COLOR}
    >
      {label}
    </InlineTokenHighlight>
  );
  if (variant === "composer") return token;
  return (
    <a
      href={`#/chat/${encodeURIComponent(mention.session_key)}`}
      className="rounded-sm underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
    >
      {token}
    </a>
  );
}

export function CliAppMentionToken({
  app,
  label,
  variant,
  isHero = false,
}: {
  app: CliAppInfo;
  label: string;
  variant: "composer" | "message";
  isHero?: boolean;
}) {
  const { t } = useTranslation();
  const color = app.brand_color || INLINE_TOKEN_HIGHLIGHT_COLOR;
  const mentionName = label.startsWith("@") ? label.slice(1) : label;
  const logoUrls = useMemo(() => logoFallbackUrls(app.logo_url), [app.logo_url]);
  const { logoUrl, onLogoError, onLogoLoad } = useLogoFallback(logoUrls);
  const showLogo = Boolean(logoUrl);
  const testIdPrefix = variant === "composer" ? "composer" : "message";

  return (
    <InlineTokenHighlight
      testId={`${testIdPrefix}-cli-mention-${app.name}`}
      title={t("thread.composer.mentions.cliTitle", { name: app.display_name || app.name })}
      color={color}
    >
      <span
        className={cn("relative inline-block", showLogo && "text-transparent")}
        style={{ lineHeight: "inherit" }}
      >
        @
        {showLogo ? (
          <span
            data-testid={`${testIdPrefix}-cli-mention-logo-${app.name}`}
            className={cn(
              "absolute left-1/2 top-1/2 grid place-items-center overflow-hidden rounded-[3px]",
              "-translate-x-1/2 -translate-y-1/2",
              isHero ? "h-[0.74em] w-[0.74em]" : "h-[0.72em] w-[0.72em]",
            )}
          >
            <img
              src={logoUrl ?? ""}
              alt=""
              className="h-full w-full object-contain"
              decoding="async"
              loading="lazy"
              onLoad={onLogoLoad}
              onError={onLogoError}
            />
          </span>
        ) : null}
      </span>
      {mentionName}
    </InlineTokenHighlight>
  );
}

export function McpPresetMentionToken({
  preset,
  label,
  variant,
  isHero = false,
}: {
  preset: McpPresetInfo;
  label: string;
  variant: "composer" | "message";
  isHero?: boolean;
}) {
  const { t } = useTranslation();
  const color = preset.brand_color || INLINE_TOKEN_HIGHLIGHT_COLOR;
  const mentionName = label.startsWith("@") ? label.slice(1) : label;
  const logoUrls = useMemo(() => logoFallbackUrls(preset.logo_url), [preset.logo_url]);
  const { logoUrl, onLogoError, onLogoLoad } = useLogoFallback(logoUrls);
  const showLogo = Boolean(logoUrl);
  const testIdPrefix = variant === "composer" ? "composer" : "message";

  return (
    <InlineTokenHighlight
      testId={`${testIdPrefix}-mcp-mention-${preset.name}`}
      title={t("thread.composer.mentions.mcpTitle", { name: preset.display_name || preset.name })}
      color={color}
    >
      <span
        className={cn("relative inline-block", showLogo && "text-transparent")}
        style={{ lineHeight: "inherit" }}
      >
        @
        {showLogo ? (
          <span
            data-testid={`${testIdPrefix}-mcp-mention-logo-${preset.name}`}
            className={cn(
              "absolute left-1/2 top-1/2 grid place-items-center overflow-hidden rounded-[3px]",
              "-translate-x-1/2 -translate-y-1/2",
              isHero ? "h-[0.74em] w-[0.74em]" : "h-[0.72em] w-[0.72em]",
            )}
          >
            <img
              src={logoUrl ?? ""}
              alt=""
              className="h-full w-full object-contain"
              decoding="async"
              loading="lazy"
              onLoad={onLogoLoad}
              onError={onLogoError}
            />
          </span>
        ) : null}
      </span>
      {mentionName}
    </InlineTokenHighlight>
  );
}

/**
 * The resource references present in *value*, in order, resolved against *targets*.
 *
 * One implementation, used both for decorating the text and for deciding what goes on the wire.
 * They were two regexes for a while and they disagreed: the decoration matched a dotted name and
 * the wire did not, so a chip appeared for a reference that was never sent.
 */
export function resourceMentionsInText(
  value: string,
  targets: ResourceMentionTarget[],
): ResourceMentionTarget[] {
  const found: ResourceMentionTarget[] = [];
  if (!value || targets.length === 0) return found;
  const seen = new Set<string>();
  for (const segment of splitCapabilityMentionSegments(value, [], [], [], targets)) {
    if (segment.kind !== "resource") continue;
    const key = `${segment.resource.kind}:${segment.resource.id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    found.push(segment.resource);
  }
  return found;
}
