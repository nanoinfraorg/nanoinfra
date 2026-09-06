/**
 * The Metrics destination — nanoinfraorg/nanoinfra#235.
 *
 * Its own place in the rail rather than a section of Settings, because Settings is where a
 * deployment is *configured* and none of these numbers are settings. Three tabs, one per surface,
 * and the reason there are three is that they answer three different questions:
 *
 * | Usage     | what this cost, per model and per day |
 * | Live      | is it healthy, right now |
 * | Calls     | what did it actually do |
 * | Approvals | is the gate working, or is somebody rubber-stamping |
 *
 * `Approvals` carries its own window control rather than sharing Usage's: it reads the gate audit
 * log, whose retention is not `llm_calls`', and one range over two retentions would mean two
 * different things in the same click.
 *
 * The strip is the shared `TabStrip`, the same one the agent editor uses. Not the segmented
 * control with counts that Automations uses to filter one list — these are surfaces, and "Usage"
 * has no number that would count it.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { MetricsApprovals } from "@/components/metrics/MetricsApprovals";
import { MetricsCalls } from "@/components/metrics/MetricsCalls";
import { MetricsLive } from "@/components/metrics/MetricsLive";
import { MetricsUsage } from "@/components/metrics/MetricsUsage";
import { TabStrip } from "@/components/ui/tab-strip";
import type { SettingsPayload } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

const TABS = ["usage", "live", "calls", "approvals"] as const;
export type MetricsTab = (typeof TABS)[number];

export function MetricsView({
  settings,
  base = "",
  onSettings,
  onOpenSession,
  knownSessions,
}: {
  settings: SettingsPayload | null;
  base?: string;
  /** Applies a refreshed payload, so a window change updates the page it came from. */
  onSettings?: (payload: SettingsPayload) => void;
  /** Opens the transcript a call row points at. */
  onOpenSession?: (sessionKey: string) => void;
  /** The sessions the shell can open, so a row for a deleted one gets no dead button. */
  knownSessions?: readonly string[];
}) {
  const { t } = useTranslation();
  // The token comes from the provider rather than a prop: this is a destination, and every other
  // one reads it the same way. The panels take it as a prop so each is testable on its own.
  const { token } = useClient();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const [tab, setTab] = useState<MetricsTab>("usage");

  const labels: Record<MetricsTab, string> = {
    usage: tx("metrics.tabs.usage", "Usage"),
    live: tx("metrics.tabs.live", "Live"),
    calls: tx("metrics.tabs.calls", "Calls"),
    approvals: tx("metrics.tabs.approvals", "Approvals"),
  };

  return (
    <div className="mx-auto w-full max-w-[64rem] px-4 py-6 sm:px-6" data-testid="metrics-view">
      <header className="mb-4">
        <h1 className="text-[22px] font-semibold leading-7 text-foreground">
          {tx("metrics.title", "Metrics")}
        </h1>
        <p className="mt-1 text-[12.5px] leading-5 text-muted-foreground">
          {tx(
            "metrics.subtitle",
            "What this deployment spent, how it is running, and what it actually did.",
          )}
        </p>
      </header>

      <TabStrip
        items={TABS.map((key) => ({ key, label: labels[key] }))}
        value={tab}
        onChange={setTab}
        ariaLabel={tx("metrics.tabs.aria", "Metrics sections")}
        testId="metrics-tab"
      />

      <div className="mt-4">
        {tab === "usage"
          ? (
            <MetricsUsage
              settings={settings}
              token={token}
              base={base}
              onSettings={onSettings}
            />
          )
          : null}
        {tab === "live" ? <MetricsLive token={token} base={base} /> : null}
        {tab === "calls"
          ? (
            <MetricsCalls
              token={token}
              base={base}
              onOpenSession={onOpenSession}
              knownSessions={knownSessions}
            />
          )
          : null}
        {tab === "approvals" ? <MetricsApprovals token={token} base={base} /> : null}
      </div>
    </div>
  );
}
