import {
  type ReactNode,
  type RefObject,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  Archive,
  Brain,
  CalendarClock,
  ChevronDown,
  KeyRound,
  Menu,
  Network,
  Search,
  Server,
  Settings,
  ShieldCheck,
  SquarePen,
  Blocks,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { ChatList } from "@/components/ChatList";
import { ConnectionBadge } from "@/components/ConnectionBadge";
import {
  SIDEBAR_SELECTION_ACTION_ITEM_CLASS,
  SidebarSelectionHighlight,
} from "@/components/SidebarSelectionHighlight";
import { Button } from "@/components/ui/button";
import type {
  ChatSummary,
  SidebarViewState,
} from "@/lib/types";
import { cn } from "@/lib/utils";

interface SidebarProps {
  sessions: ChatSummary[];
  activeKey: string | null;
  loading: boolean;
  newChatActive: boolean;
  onNewChat: () => void;
  onSelect: (key: string) => void;
  onRequestDelete: (key: string, label: string) => void;
  onTogglePin: (key: string) => void;
  onRequestRename: (key: string, label: string) => void;
  onToggleArchive: (key: string) => void;
  onReorderSessions: (keys: string[]) => void;
  onToggleGroup: (groupId: string) => void;
  onRequestRenameProject: (projectKey: string, label: string) => void;
  onNewChatInProject: (projectPath: string, projectName: string) => void;
  onOpenSettings: () => void;
  onOpenApps: () => void;
  onOpenSkills: () => void;
  onOpenAutomations: () => void;
  onOpenDiagrams: () => void;
  onOpenServers: () => void;
  onOpenSecrets: () => void;
  onOpenApprovals: () => void;
  /** How many actions wait for a human answer (nanoinfraorg/nanoinfra#27). */
  approvalsCount?: number;
  /** The running build, from the settings payload. Absent until it loads, or on an older gateway. */
  version?: string;
  onSettingsIntent?: () => void;
  onOpenSearch: () => void;
  activeUtility?:
    | "apps"
    | "skills"
    | "automations"
    | "diagrams"
    | "servers"
    | "secrets"
    | "approvals"
    | null;
  onToggleArchived: () => void;
  onCollapse: () => void;
  onExpand?: () => void;
  containActionMenus?: boolean;
  collapsed?: boolean;
  pinnedKeys?: string[];
  archivedKeys?: string[];
  sessionOrder?: string[];
  titleOverrides?: Record<string, string>;
  projectNameOverrides?: Record<string, string>;
  collapsedGroups?: Record<string, boolean>;
  runningChatIds?: string[];
  updatedChatIds?: string[];
  viewState?: SidebarViewState;
  showArchived?: boolean;
  archivedCount?: number;
  defaultWorkspacePath?: string | null;
  hostChromeInset?: boolean;
}

type NavigatorWithUserAgentData = Navigator & {
  userAgentData?: { platform?: string };
};

function isApplePlatform(): boolean {
  if (typeof navigator === "undefined") return false;
  const platform = navigator.platform || "";
  const userAgentPlatform =
    (navigator as NavigatorWithUserAgentData).userAgentData?.platform || "";
  return /mac|iphone|ipad|ipod/i.test(`${platform} ${userAgentPlatform}`);
}

function newChatShortcutLabel(): string {
  return isApplePlatform() ? "⌘⇧O" : "Ctrl+Shift+O";
}

export function Sidebar(props: SidebarProps) {
  const { t } = useTranslation();
  const [menuPortalContainer, setMenuPortalContainer] =
    useState<HTMLElement | null>(null);
  const [infraExpanded, setInfraExpanded] = useState(
    () =>
      props.activeUtility === "diagrams"
      || props.activeUtility === "servers"
      || props.activeUtility === "secrets",
  );
  useEffect(() => {
    if (
      props.activeUtility === "diagrams"
      || props.activeUtility === "servers"
      || props.activeUtility === "secrets"
    ) {
      setInfraExpanded(true);
    }
  }, [props.activeUtility]);
  const collapsed = Boolean(props.collapsed);
  const toggleLabel = t("thread.header.toggleSidebar");
  const newChatShortcut = newChatShortcutLabel();
  const activeActionRef = useRef<HTMLButtonElement>(null);
  const activeActionId = props.newChatActive
    ? "new-chat"
    : props.activeUtility
      ? `utility:${props.activeUtility}`
      : null;

  return (
    <nav
      ref={props.containActionMenus ? setMenuPortalContainer : undefined}
      aria-label={t("sidebar.navigation")}
      className={cn(
        "flex h-full w-full min-w-0 flex-col text-sidebar-foreground",
        props.hostChromeInset ? "bg-transparent" : "bg-sidebar",
      )}
    >
      <div
        className={cn(
          "flex items-center px-3 pb-2.5",
          props.hostChromeInset ? "pt-[2.85rem]" : "pt-3",
          collapsed ? "w-14 justify-start" : "justify-between",
        )}
      >
        <button
          type="button"
          aria-label={collapsed ? toggleLabel : undefined}
          aria-hidden={collapsed ? undefined : true}
          title={collapsed ? toggleLabel : undefined}
          onClick={collapsed ? props.onExpand : undefined}
          tabIndex={collapsed ? 0 : -1}
          className={cn(
            // The width follows the asset. A fixed 9 holds the square mark. ``w-auto``
            // lets the wordmark take the width its shape asks for.
            "-ml-0.5 flex h-9 shrink-0 items-center justify-center overflow-hidden rounded-xl transition-colors",
            collapsed
              ? "w-9 hover:bg-sidebar-accent/75"
              : "w-auto pointer-events-none",
          )}
        >
          {/*
           * The header shows one brand asset at a time, and the rail width selects it.
           *
           * The open rail is 272 px wide. The wordmark goes there, because it states the
           * product name. It replaces the square mark, and the header never shows both.
           *
           * The collapsed rail is 56 px wide. The wordmark is 1300 x 220, so it asks for
           * 165 px at this height. It misses by 109 px. The square mark stays in the rail
           * for that reason, and not for taste.
           *
           * The height is fixed and the width is free. A fixed width squashes a 5.909:1
           * asset, so the height alone sets the size.
           *
           * ``alt`` stays empty for both assets, because the image is decoration. A
           * collapsed rail gets its name from the button label, which names the control
           * and not the product. An open rail makes this button inert, so nothing here
           * needs a name. The product name reaches a screen reader from the document
           * title one time.
           */}
          <img
            src={collapsed ? "/brand/nanoinfra_mark.svg" : "/brand/nanoinfra_wordmark.svg"}
            alt=""
            className={cn(
              "select-none object-contain",
              collapsed ? "h-8 w-8" : "h-7 w-auto",
            )}
            draggable={false}
          />
        </button>
        {!collapsed && !props.hostChromeInset && (
          <Button
            variant="ghost"
            size="icon"
            aria-label={t("sidebar.collapse")}
            onClick={props.onCollapse}
            className="h-7 w-7 rounded-lg text-muted-foreground/85 hover:bg-sidebar-accent/75 hover:text-sidebar-foreground"
          >
            <Menu className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>

      <SidebarSelectionHighlight
        targetRef={activeActionRef}
        activeId={activeActionId}
        scope="actions"
        className={cn(
          "relative space-y-1.5 px-2 pb-2",
          collapsed && "flex w-14 flex-col items-center px-0",
        )}
      >
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.newChat")}
          onClick={props.onNewChat}
          active={props.newChatActive}
          selectionRef={activeActionRef}
          icon={<SquarePen className="h-4 w-4" />}
          shortcut={newChatShortcut}
          ariaKeyShortcuts="Meta+Shift+O Control+Shift+O"
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.searchAria")}
          onClick={props.onOpenSearch}
          icon={<Search className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.apps")}
          onClick={props.onOpenApps}
          onIntent={props.onSettingsIntent}
          active={props.activeUtility === "apps"}
          selectionRef={activeActionRef}
          icon={<Blocks className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.skills.title")}
          onClick={props.onOpenSkills}
          onIntent={props.onSettingsIntent}
          active={props.activeUtility === "skills"}
          selectionRef={activeActionRef}
          icon={<Brain className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.automations", { defaultValue: "Automations" })}
          onClick={props.onOpenAutomations}
          onIntent={props.onSettingsIntent}
          active={props.activeUtility === "automations"}
          selectionRef={activeActionRef}
          icon={<CalendarClock className="h-4 w-4" />}
        />
        <SidebarActionButton
          collapsed={collapsed}
          label={t("sidebar.approvals", { defaultValue: "Approvals" })}
          ariaLabel={
            props.approvalsCount
              ? t("sidebar.approvalsBadge", {
                count: props.approvalsCount,
                defaultValue: "Approvals waiting for an answer: {{count}}",
              })
              : undefined
          }
          onClick={props.onOpenApprovals}
          active={props.activeUtility === "approvals"}
          selectionRef={activeActionRef}
          icon={<ShieldCheck className="h-4 w-4" />}
          trailing={
            props.approvalsCount
              ? (
                <span className="rounded-full bg-destructive px-1.5 text-[10px] font-semibold leading-4 text-destructive-foreground">
                  {props.approvalsCount}
                </span>
              )
              : undefined
          }
        />
        {collapsed ? (
          <>
            <SidebarActionButton
              collapsed={collapsed}
              label={t("sidebar.diagrams", { defaultValue: "Diagrams" })}
              onClick={props.onOpenDiagrams}
              onIntent={props.onSettingsIntent}
              active={props.activeUtility === "diagrams"}
              selectionRef={activeActionRef}
              icon={<Network className="h-4 w-4" />}
            />
            <SidebarActionButton
              collapsed={collapsed}
              label={t("sidebar.servers", { defaultValue: "Servers" })}
              onClick={props.onOpenServers}
              onIntent={props.onSettingsIntent}
              active={props.activeUtility === "servers"}
              selectionRef={activeActionRef}
              icon={<Server className="h-4 w-4" />}
            />
            <SidebarActionButton
              collapsed={collapsed}
              label={t("sidebar.secrets", { defaultValue: "Secrets" })}
              onClick={props.onOpenSecrets}
              onIntent={props.onSettingsIntent}
              active={props.activeUtility === "secrets"}
              selectionRef={activeActionRef}
              icon={<KeyRound className="h-4 w-4" />}
            />
          </>
        ) : (
          <div>
            <SidebarActionButton
              collapsed={false}
              label={t("sidebar.infrastructure", { defaultValue: "Infrastructure" })}
              onClick={() => setInfraExpanded((v) => !v)}
              ariaExpanded={infraExpanded}
              icon={<Network className="h-4 w-4" />}
              trailing={
                <ChevronDown
                  className={cn(
                    "h-3.5 w-3.5 text-muted-foreground transition-transform",
                    infraExpanded && "rotate-180",
                  )}
                />
              }
            />
            {infraExpanded ? (
              <div className="ml-3 mt-0.5 flex flex-col gap-0.5 border-l border-border/45 pl-3">
                <SidebarActionButton
                  collapsed={false}
                  label={t("sidebar.diagrams", { defaultValue: "Diagrams" })}
                  onClick={props.onOpenDiagrams}
                  onIntent={props.onSettingsIntent}
                  active={props.activeUtility === "diagrams"}
                  selectionRef={activeActionRef}
                  icon={<Network className="h-4 w-4" />}
                />
                <SidebarActionButton
                  collapsed={false}
                  label={t("sidebar.servers", { defaultValue: "Servers" })}
                  onClick={props.onOpenServers}
                  onIntent={props.onSettingsIntent}
                  active={props.activeUtility === "servers"}
                  selectionRef={activeActionRef}
                  icon={<Server className="h-4 w-4" />}
                />
                <SidebarActionButton
                  collapsed={false}
                  label={t("sidebar.secrets", { defaultValue: "Secrets" })}
                  onClick={props.onOpenSecrets}
                  onIntent={props.onSettingsIntent}
                  active={props.activeUtility === "secrets"}
                  selectionRef={activeActionRef}
                  icon={<KeyRound className="h-4 w-4" />}
                />
              </div>
            ) : null}
          </div>
        )}
        {props.archivedCount ? (
          <SidebarActionButton
            collapsed={collapsed}
            label={props.showArchived ? t("chat.hideArchived") : t("chat.showArchived")}
            onClick={props.onToggleArchived}
            icon={<Archive className="h-4 w-4" />}
          />
        ) : null}
      </SidebarSelectionHighlight>
      <div
        className={cn(
          "flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden transition-opacity duration-200",
          collapsed && "pointer-events-none opacity-0",
        )}
      >
        {!collapsed && (
          <ChatList
            sessions={props.sessions}
            activeKey={props.activeKey}
            loading={props.loading}
            emptyLabel={t("chat.noSessions")}
            onSelect={props.onSelect}
            onRequestDelete={props.onRequestDelete}
            onTogglePin={props.onTogglePin}
            onRequestRename={props.onRequestRename}
            onToggleArchive={props.onToggleArchive}
            onReorderSessions={props.onReorderSessions}
            onToggleGroup={props.onToggleGroup}
            onRequestRenameProject={props.onRequestRenameProject}
            onNewChatInProject={props.onNewChatInProject}
            pinnedKeys={props.pinnedKeys}
            archivedKeys={props.archivedKeys}
            sessionOrder={props.sessionOrder}
            titleOverrides={props.titleOverrides}
            projectNameOverrides={props.projectNameOverrides}
            collapsedGroups={props.collapsedGroups}
            runningChatIds={props.runningChatIds}
            updatedChatIds={props.updatedChatIds}
            density={props.viewState?.density}
            showPreviews={props.viewState?.show_previews}
            showTimestamps={props.viewState?.show_timestamps}
            sort={props.viewState?.sort}
            showArchived={props.showArchived}
            defaultWorkspacePath={props.defaultWorkspacePath}
            actionMenuPortalContainer={
              props.containActionMenus ? menuPortalContainer : undefined
            }
          />
        )}
      </div>
      <div
        className={cn(
          "bg-sidebar/55 px-2.5 py-3 text-xs",
          collapsed && "w-14 px-0",
        )}
      >
        <div className={cn("flex items-center gap-1", collapsed && "flex-col")}>
          <SidebarActionButton
            collapsed={collapsed}
            label={t("sidebar.settings")}
            onClick={props.onOpenSettings}
            onIntent={props.onSettingsIntent}
            className={collapsed ? undefined : "flex-1"}
            icon={<Settings className="h-4 w-4" />}
          />
          <ConnectionBadge />
        </div>
        {collapsed ? null : <SidebarVersion version={props.version} />}
      </div>
    </nav>
  );
}

/** A released version, and nothing else, gets a link to its notes. */
const RELEASE_VERSION = /^\d+\.\d+\.\d+$/;

const RELEASES_URL = "https://github.com/nanoinfraorg/nanoinfra/releases/tag/v";

/**
 * The build that is running, under Settings.
 *
 * A local build carries a version this project never published -- `0.16.0.dev3+g1a2b3c4` from a
 * source checkout -- and linking that to a release page would send a reader to a 404. So only an
 * exact `x.y.z` becomes a link, and anything else stays text that still answers "which build is
 * this?".
 */
function SidebarVersion({ version }: { version?: string }) {
  const { t } = useTranslation();
  const value = (version ?? "").trim();
  if (!value) return null;
  const label = `nanoinfra ${value}`;
  // `px-3` is the action row's own horizontal padding, so this line starts on the same vertical
  // edge as the gear icon above it and as every nav icon in the rail. `px-2` read as misaligned by
  // exactly the 4px difference, which is enough to see and not enough to explain.
  if (!RELEASE_VERSION.test(value)) {
    return (
      <div className="mt-2 px-3 text-[11px] leading-4 text-sidebar-foreground/45">{label}</div>
    );
  }
  return (
    <div className="mt-2 px-3 text-[11px] leading-4">
      <a
        href={`${RELEASES_URL}${value}`}
        target="_blank"
        rel="noreferrer"
        title={t("sidebar.releaseNotes", { defaultValue: "Release notes for v{{version}}", version: value })}
        className="text-sidebar-foreground/45 underline-offset-2 transition-colors hover:text-sidebar-foreground/75 hover:underline"
      >
        {label}
      </a>
    </div>
  );
}

function SidebarActionButton({
  collapsed,
  label,
  icon,
  onClick,
  active = false,
  className,
  shortcut,
  ariaKeyShortcuts,
  ariaLabel,
  onIntent,
  selectionRef,
  trailing,
  ariaExpanded,
}: {
  collapsed: boolean;
  label: string;
  icon: ReactNode;
  onClick: () => void;
  active?: boolean;
  className?: string;
  shortcut?: string;
  ariaKeyShortcuts?: string;
  /** Overrides the label for a screen reader, e.g. a row that carries an unread count. */
  ariaLabel?: string;
  onIntent?: () => void;
  selectionRef?: RefObject<HTMLButtonElement>;
  /** Extra content after the label, e.g. a disclosure chevron. Hidden when collapsed, same as the label. */
  trailing?: ReactNode;
  ariaExpanded?: boolean;
}) {
  const title = shortcut ? `${label} (${shortcut})` : collapsed ? label : undefined;

  return (
    <Button
      ref={active ? selectionRef : undefined}
      type="button"
      variant={null}
      aria-label={ariaLabel ?? label}
      aria-current={active ? "page" : undefined}
      aria-expanded={ariaExpanded}
      aria-keyshortcuts={ariaKeyShortcuts}
      title={title}
      onClick={() => onClick()}
      onFocus={onIntent}
      onPointerEnter={onIntent}
      className={cn(
        "touch-target group h-8 min-w-0 gap-2 overflow-hidden rounded-xl font-medium",
        SIDEBAR_SELECTION_ACTION_ITEM_CLASS,
        collapsed
          ? "w-9 justify-center gap-0 px-0"
          : "w-full justify-start gap-2 px-3 text-[12.5px]",
        active
          ? "text-sidebar-accent-foreground"
          : "text-sidebar-foreground/85 hover:bg-sidebar-foreground/[0.035] hover:text-sidebar-foreground dark:hover:bg-white/[0.05]",
        className,
      )}
    >
      <span
        className={cn(
          "flex shrink-0 items-center justify-center transition-transform duration-300 ease-out",
          collapsed ? "translate-x-0" : "translate-x-0",
        )}
        aria-hidden
      >
        {icon}
      </span>
      <span
        className={cn(
          "min-w-0 overflow-hidden truncate whitespace-nowrap transition-[max-width,opacity,transform] duration-200 ease-out",
          collapsed
            ? "max-w-0 -translate-x-1 opacity-0"
            : "max-w-[12rem] translate-x-0 opacity-100",
        )}
      >
        {label}
      </span>
      {trailing && !collapsed ? (
        // ml-auto pushes just this element (not the label) to the row's far
        // end, so the label keeps its normal left-hugging/truncate behavior
        // identical to every other row -- only rows that pass `trailing`
        // render this at all.
        <span className="ml-auto flex shrink-0 items-center justify-center" aria-hidden>
          {trailing}
        </span>
      ) : null}
    </Button>
  );
}
