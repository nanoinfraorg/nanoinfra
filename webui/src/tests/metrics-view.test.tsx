/**
 * The Metrics destination — nanoinfraorg/nanoinfra#235.
 *
 * Three tabs, three different failure modes worth pinning:
 *
 * - **Usage** must not print `$0.00` when nothing is priced. `0` is a price, and a month of real
 *   spend rendered as free is worse than a blank.
 * - **Live** must not print `0` for a gauge it could not read. "The executor did not answer" and
 *   "nothing is pending" are different facts.
 * - **Calls** must not print the arguments, and must page with the keyset cursor rather than an
 *   offset.
 *
 * The heatmap and summary assertions here used to live in `settings-view.test.tsx`, against the
 * band on the Settings overview. They moved with the components.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";

import { MetricsApprovals } from "@/components/metrics/MetricsApprovals";
import { MetricsCalls } from "@/components/metrics/MetricsCalls";
import { MetricsLive } from "@/components/metrics/MetricsLive";
import { MetricsUsage } from "@/components/metrics/MetricsUsage";
import { MetricsView } from "@/components/metrics/MetricsView";
import { ClientProvider } from "@/providers/ClientProvider";
import type { MetricsCallsPayload, SettingsPayload } from "@/lib/types";

function jsonResponse(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}

type Usage = NonNullable<SettingsPayload["usage"]>;

function usagePayload(overrides: Partial<Usage> = {}): Usage {
  return {
    days: [
      {
        date: "2026-06-03",
        prompt_tokens: 1200,
        completion_tokens: 300,
        cached_tokens: 500,
        total_tokens: 1500,
        requests: 2,
      },
    ],
    total_tokens: 1500,
    total_tokens_30d: 1500,
    total_tokens_365d: 1500,
    peak_day_tokens: 1500,
    current_streak_days: 1,
    longest_streak_days: 1,
    active_days_30d: 1,
    requests_30d: 2,
    window_days: 30,
    updated_at: "2026-06-03T00:00:00Z",
    ...overrides,
  };
}

function settingsWith(usage: Usage, timezone = "UTC"): SettingsPayload {
  // Only the two fields these components read. The full payload is a hundred keys and none of the
  // others reach this page.
  return {
    agent: { timezone },
    usage,
  } as unknown as SettingsPayload;
}

function callsPayload(overrides: Partial<MetricsCallsPayload> = {}): MetricsCallsPayload {
  return {
    calls: [
      {
        id: 42,
        ts_ms: Date.UTC(2026, 5, 3, 12, 0, 0),
        session_key: "webui:main",
        turn_id: "turn-7",
        seq: 1,
        tool: "exec",
        source: "user",
        actor: "alberto",
        capability_class: "shell",
        gate_decision: "allow",
        gate_reason: "standing grant",
        outcome: "ok",
        duration_ms: 2400,
        error_kind: null,
      },
    ],
    has_more: false,
    next_before_id: null,
    tools: ["exec", "read_file"],
    outcomes: ["ok", "error", "denied"],
    gate_decisions: ["allow", "deny"],
    retention_days: 180,
    last_purge: null,
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("MetricsUsage", () => {
  it("shows the token summary and the heatmap the Settings overview used to carry", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));

    render(
      <MetricsUsage settings={settingsWith(usagePayload())} token="tok" />,
    );

    expect(await screen.findByLabelText("Token activity")).toBeInTheDocument();
    expect(screen.getByText("Token Usage")).toBeInTheDocument();
  });

  it("aligns heatmap days with the configured timezone", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-02T18:00:00Z"));
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));

    render(
      <MetricsUsage
        settings={settingsWith(usagePayload(), "Asia/Shanghai")}
        token="tok"
      />,
    );

    expect(screen.getByLabelText("2026-06-03: 1.5K tokens, 2 requests")).toBeInTheDocument();
  });

  it("says no prices are configured rather than showing a spend of zero", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));

    render(
      <MetricsUsage
        settings={settingsWith(usagePayload({ cost_usd_window: null, priced_models: 0 }))}
        token="tok"
      />,
    );

    expect(screen.getByText("No prices configured")).toBeInTheDocument();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("shows the window's spend when a price exists", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));

    render(
      <MetricsUsage
        settings={settingsWith(usagePayload({ cost_usd_window: 12.5, priced_models: 1 }))}
        token="tok"
      />,
    );

    expect(screen.getByText("$12.50")).toBeInTheDocument();
  });

  it("refetches with the chosen window rather than re-slicing what it holds", async () => {
    // Route-aware, because the tab now also loads the scale row (#274) and answering that route
    // with a usage payload is not a case worth asserting here.
    const fetchMock = vi.fn(async (input: RequestInfo | URL) =>
      String(input).includes("/metrics/scale")
        ? jsonResponse({
          servers: 1,
          skills: 2,
          agents: 1,
          mcp_servers: 0,
          connectors: 0,
          unavailable: [],
        })
        : jsonResponse(usagePayload({ window_days: 90 }))
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(
      <MetricsUsage settings={settingsWith(usagePayload())} token="tok" />,
    );

    await user.click(screen.getByRole("button", { name: "90d" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([input]) =>
          String(input) === "/api/settings/usage?window=90"
        ),
      ).toBe(true);
    });
  });

  it("shows the columns the store recorded and never displayed", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));

    render(
      <MetricsUsage
        settings={settingsWith(usagePayload({
          providers_30d: [{
            provider: "openai",
            model: "gpt-4o",
            total_tokens: 1500,
            prompt_tokens: 1000,
            completion_tokens: 300,
            cached_tokens: 200,
            provider_tokens: 1500,
            estimated_tokens: 0,
            requests: 2,
            failed_requests: 0,
            ttft_ms: 600,
            timed_requests: 2,
            generation_ms: 1800,
            measured_completion_tokens: 300,
            cache_write_tokens: 120,
            duration_ms: 4200,
            truncated_requests: 1,
            streamed_requests: 2,
            cost_usd: 0.42,
          }],
        }))}
        token="tok"
      />,
    );

    const table = screen.getByTestId("metrics-models");
    // Each of these five was written by the store and reached no pixel before #235.
    expect(within(table).getByText("Cache write")).toBeInTheDocument();
    expect(within(table).getByText("Truncated")).toBeInTheDocument();
    expect(within(table).getByText("Wall clock")).toBeInTheDocument();
    expect(within(table).getByText("TTFT")).toBeInTheDocument();
    expect(within(table).getByText("Cost")).toBeInTheDocument();
  });

  it("breaks the window down by what started the turns", () => {
    // `source` was aggregated per day and readable only inside a heatmap cell's tooltip, so
    // "what does automation cost me this month" had no surface that answered it.
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));

    render(
      <MetricsUsage
        settings={settingsWith(usagePayload({
          sources_window: [
            {
              source: "cron",
              total_tokens: 6300,
              prompt_tokens: 6000,
              completion_tokens: 300,
              cached_tokens: 0,
              requests: 2,
              failed_requests: 0,
            },
            {
              source: "user",
              total_tokens: 1100,
              prompt_tokens: 1000,
              completion_tokens: 100,
              cached_tokens: 0,
              requests: 1,
              failed_requests: 0,
            },
          ],
        }))}
        token="tok"
      />,
    );

    const band = screen.getByTestId("metrics-sources");
    expect(within(band).getByText("Automations")).toBeInTheDocument();
    expect(within(band).getByText("6,300")).toBeInTheDocument();
    expect(within(band).getByText("Chat")).toBeInTheDocument();
  });

  it("expands a model row to the measurements the ten columns are checked against", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
    const user = userEvent.setup();

    render(
      <MetricsUsage
        settings={settingsWith(usagePayload({
          providers_30d: [{
            provider: "openai",
            model: "gpt-4o",
            total_tokens: 1500,
            prompt_tokens: 1000,
            completion_tokens: 300,
            cached_tokens: 200,
            provider_tokens: 1200,
            estimated_tokens: 300,
            requests: 4,
            failed_requests: 0,
            ttft_ms: 1200,
            timed_requests: 2,
            generation_ms: 3000,
            measured_completion_tokens: 290,
            cache_write_tokens: 120,
            duration_ms: 8400,
            truncated_requests: 0,
            streamed_requests: 2,
            provider_requests: 3,
            estimated_requests: 1,
            cost_usd: 0.42,
          }],
        }))}
        token="tok"
      />,
    );

    await user.click(screen.getByTestId("metrics-model-gpt-4o"));

    const detail = screen.getByTestId("metrics-model-detail-gpt-4o");
    // Four measurements, each stored by the store and displayed nowhere before #235.
    expect(within(detail).getByText("Reported / estimated")).toBeInTheDocument();
    expect(within(detail).getByText(/3 of 4 calls reported by the provider/)).toBeInTheDocument();
    // 300 output tokens over 3s of generation.
    expect(within(detail).getByText("100.0 output tok/s")).toBeInTheDocument();
    expect(within(detail).getByText("290 / 300")).toBeInTheDocument();
    expect(within(detail).getByText("2 / 2")).toBeInTheDocument();
  });

  it("says a dash for throughput rather than dividing by zero", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
    const user = userEvent.setup();

    render(
      <MetricsUsage
        settings={settingsWith(usagePayload({
          providers_30d: [{
            provider: "ollama",
            model: "qwen3",
            total_tokens: 10,
            prompt_tokens: 8,
            completion_tokens: 2,
            cached_tokens: 0,
            provider_tokens: 0,
            estimated_tokens: 10,
            requests: 1,
            failed_requests: 0,
            ttft_ms: 0,
            timed_requests: 0,
            generation_ms: 0,
            measured_completion_tokens: 0,
            cost_usd: null,
          }],
        }))}
        token="tok"
      />,
    );

    await user.click(screen.getByTestId("metrics-model-qwen3"));

    const detail = screen.getByTestId("metrics-model-detail-qwen3");
    expect(within(detail).getByText("—")).toBeInTheDocument();
  });

  it("lists why calls failed, which the counts alone never said", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));

    render(
      <MetricsUsage
        settings={settingsWith(usagePayload({
          failed_requests_30d: 3,
          failures: [
            { error_kind: "rate_limit", status_code: 429, provider: "openai", requests: 3 },
          ],
        }))}
        token="tok"
      />,
    );

    expect(screen.getByText("Why calls failed")).toBeInTheDocument();
    expect(screen.getByText(/rate_limit/)).toBeInTheDocument();
    expect(screen.getByText(/429/)).toBeInTheDocument();
  });
});

describe("MetricsLive", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  it("renders a dash for a gauge it could not read, not a zero", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          available: true,
          gauges: [
            {
              name: "nanoinfra_pending_approvals",
              value: null,
              label: "Approvals waiting",
              help: "Suspended actions waiting for a person to answer.",
              alerting: true,
            },
          ],
        })
      ),
    );

    render(<MetricsLive token="tok" />);

    const card = await screen.findByTestId("metrics-gauge-nanoinfra_pending_approvals");
    expect(within(card).getByText("—")).toBeInTheDocument();
    expect(within(card).queryByText("0")).not.toBeInTheDocument();
  });

  it("raises the alerting gauge when somebody is actually waiting", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          available: true,
          gauges: [
            {
              name: "nanoinfra_pending_approvals",
              value: 2,
              label: "Approvals waiting",
              help: "Suspended actions waiting for a person to answer.",
              alerting: true,
            },
            {
              name: "nanoinfra_ws_connections",
              value: 0,
              label: "WebUI sockets",
              help: "Open WebSocket connections from the WebUI.",
              alerting: false,
            },
          ],
        })
      ),
    );

    render(<MetricsLive token="tok" />);

    const raised = await screen.findByTestId("metrics-gauge-nanoinfra_pending_approvals");
    expect(raised.className).toContain("amber");
    // A zero on a non-alerting gauge is a health sign, not a warning.
    const calm = screen.getByTestId("metrics-gauge-nanoinfra_ws_connections");
    expect(calm.className).not.toContain("amber");
  });

  it("says the numbers are not here rather than rendering seven blanks", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ gauges: [], available: false })),
    );

    render(<MetricsLive token="tok" />);

    expect(await screen.findByTestId("metrics-live-unavailable")).toBeInTheDocument();
    expect(screen.queryByTestId("metrics-live")).not.toBeInTheDocument();
  });

  it("keeps the last sample on screen when a poll fails", async () => {
    // Route-aware: the tab now also loads the counters charts, and a mock that counts calls
    // would fail whichever component happened to mount its effect first.
    let liveReads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (!url.includes("/metrics/live")) return jsonResponse({ counters: [], histograms: [] });
        liveReads += 1;
        if (liveReads === 1) {
          return jsonResponse({
            available: true,
            gauges: [{
              name: "nanoinfra_ws_connections",
              value: 3,
              label: "WebUI sockets",
              help: "Open WebSocket connections from the WebUI.",
              alerting: false,
            }],
          });
        }
        throw new Error("gateway went away");
      }),
    );

    render(<MetricsLive token="tok" />);
    await screen.findByText("3");

    await vi.advanceTimersByTimeAsync(3_100);

    await waitFor(() => {
      expect(screen.getByTestId("metrics-live-error")).toBeInTheDocument();
    });
    // The number is still there. A panel that blanks on a dropped poll teaches people to ignore it.
    expect(screen.getByText("3")).toBeInTheDocument();
  });
});

describe("MetricsCalls", () => {
  it("shows a call's address and never its arguments", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(callsPayload())));
    const user = userEvent.setup();

    render(<MetricsCalls token="tok" />);

    await user.click(await screen.findByTestId("metrics-call-42"));

    const detail = screen.getByTestId("metrics-call-detail");
    expect(within(detail).getByText("webui:main")).toBeInTheDocument();
    expect(within(detail).getByText("turn-7")).toBeInTheDocument();
    expect(
      within(detail).getByText(/This row is the address of the call/),
    ).toBeInTheDocument();
  });

  it("sends every filter as a query parameter", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(callsPayload()));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<MetricsCalls token="tok" />);
    await screen.findByTestId("metrics-call-42");

    await user.selectOptions(screen.getByLabelText("Outcome"), "denied");

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([input]) => String(input).includes("outcome=denied")),
      ).toBe(true);
    });
  });

  it("pages with the keyset cursor and appends rather than replacing", async () => {
    const second = callsPayload({
      calls: [{ ...callsPayload().calls[0], id: 7, tool: "read_file" }],
      has_more: false,
      next_before_id: null,
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) =>
      String(input).includes("before=42")
        ? jsonResponse(second)
        : jsonResponse(callsPayload({ has_more: true, next_before_id: 42 }))
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<MetricsCalls token="tok" />);
    await user.click(await screen.findByTestId("metrics-calls-more"));

    // Both pages on screen: a cursor page appends, and the first page is not thrown away.
    expect(await screen.findByTestId("metrics-call-7")).toBeInTheDocument();
    expect(screen.getByTestId("metrics-call-42")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.every(([input]) => !String(input).includes("offset")),
    ).toBe(true);
  });

  it("states the retention window, so an empty page is not read as an idle one", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(callsPayload({
          calls: [],
          last_purge: {
            ts_ms: Date.UTC(2026, 5, 1, 3, 0, 0),
            rows_purged: 918,
            cutoff_ms: Date.UTC(2025, 11, 3, 0, 0, 0),
          },
        }))
      ),
    );

    render(<MetricsCalls token="tok" />);

    expect(await screen.findByTestId("metrics-calls-empty")).toBeInTheDocument();
    const retention = screen.getByTestId("metrics-calls-retention");
    expect(retention.textContent).toContain("180");
    expect(retention.textContent).toContain("918");
  });

  it("offers the transcript only for a session the shell can still open", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(callsPayload())));
    const onOpenSession = vi.fn();
    const user = userEvent.setup();

    const { unmount } = render(
      <MetricsCalls token="tok" onOpenSession={onOpenSession} knownSessions={["webui:main"]} />,
    );
    await user.click(await screen.findByTestId("metrics-call-42"));
    await user.click(screen.getByTestId("metrics-call-open-session"));
    expect(onOpenSession).toHaveBeenCalledWith("webui:main");
    unmount();

    // The same row, for a session that has since been deleted: no button rather than a dead one.
    render(<MetricsCalls token="tok" onOpenSession={onOpenSession} knownSessions={[]} />);
    await user.click(await screen.findByTestId("metrics-call-42"));
    expect(screen.queryByTestId("metrics-call-open-session")).not.toBeInTheDocument();
  });

  it("does not read the log as empty when the route fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new Error("no store");
    }));

    render(<MetricsCalls token="tok" />);

    expect(await screen.findByTestId("metrics-calls-error")).toBeInTheDocument();
    expect(screen.queryByTestId("metrics-calls-empty")).not.toBeInTheDocument();
  });
});

describe("MetricsView", () => {
  it("switches between its four tabs", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/metrics/live")) {
          return jsonResponse({ available: true, gauges: [] });
        }
        if (url.includes("/metrics/counters")) {
          return jsonResponse({ counters: [], histograms: [] });
        }
        if (url.includes("/metrics/scale")) {
          return jsonResponse({
            servers: 0, skills: 0, agents: 0, mcp_servers: 0, connectors: 0, unavailable: [],
          });
        }
        return jsonResponse(callsPayload());
      }),
    );
    const user = userEvent.setup();

    render(
      <ClientProvider client={{} as never} token="tok">
        <MetricsView settings={settingsWith(usagePayload())} />
      </ClientProvider>,
    );

    expect(screen.getByTestId("metrics-view")).toBeInTheDocument();
    expect(await screen.findByLabelText("Token activity")).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Calls" }));
    expect(await screen.findByTestId("metrics-calls")).toBeInTheDocument();
    expect(screen.queryByLabelText("Token activity")).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Live" }));
    expect(await screen.findByTestId("metrics-live")).toBeInTheDocument();
  });
});

describe("MetricsScaleRow", () => {
  it("shows the five counts a reader would otherwise open five pages for", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/metrics/scale")
          ? jsonResponse({
            servers: 6,
            skills: 14,
            agents: 2,
            mcp_servers: 3,
            connectors: 1,
            unavailable: [],
          })
          : new Promise<Response>(() => {})
      ),
    );

    render(<MetricsUsage settings={settingsWith(usagePayload())} token="tok" />);

    const row = await screen.findByTestId("metrics-scale");
    expect(within(row).getByText("6")).toBeInTheDocument();
    expect(within(row).getByText("14")).toBeInTheDocument();
    expect(screen.queryByTestId("metrics-scale-unavailable")).not.toBeInTheDocument();
  });

  it("renders a dash for a count it could not read, and says so", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/metrics/scale")
          ? jsonResponse({
            servers: null,
            skills: 14,
            agents: 2,
            mcp_servers: 0,
            connectors: 0,
            unavailable: ["servers"],
          })
          : new Promise<Response>(() => {})
      ),
    );

    render(<MetricsUsage settings={settingsWith(usagePayload())} token="tok" />);

    const servers = await screen.findByTestId("metrics-scale-servers");
    expect(within(servers).getByText("—")).toBeInTheDocument();
    expect(screen.getByTestId("metrics-scale-unavailable")).toBeInTheDocument();
  });

  it("does not take the Usage tab down when the payload is a shape it did not expect", async () => {
    /*
     * This row renders inside Usage, and it used to read `scale.unavailable.length` without
     * checking. An older gateway answering the route without that field threw, React unmounted
     * the tree, and every number on the tab vanished — a worse outcome than no row at all.
     */
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ what: "is this" })),
    );

    render(<MetricsUsage settings={settingsWith(usagePayload())} token="tok" />);

    // The tab is still there, which is the whole assertion.
    expect(await screen.findByTestId("metrics-usage")).toBeInTheDocument();
    expect(screen.getByTestId("metrics-window")).toBeInTheDocument();
    expect(screen.queryByTestId("metrics-scale")).not.toBeInTheDocument();
  });
});

describe("MetricsApprovals", () => {
  const approvals = (over: Record<string, unknown> = {}) => ({
    window_days: 30,
    asked: 43,
    answered: 38,
    refused: 1,
    expired: 3,
    unanswered: 1,
    policy_refusals: 16,
    refusal_share: 0.0256,
    same_path_answers: 0,
    median_seconds_to_answer: 13.3,
    fastest_seconds: 3.6,
    slowest_seconds: 80.6,
    attributed_to: "ask",
    ...over,
  });

  it("shows the four numbers and the median", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(approvals())));

    render(<MetricsApprovals token="tok" />);

    expect(within(await screen.findByTestId("metrics-approvals-asked")).getByText("43"))
      .toBeInTheDocument();
    expect(within(screen.getByTestId("metrics-approvals-answered")).getByText("38"))
      .toBeInTheDocument();
    expect(within(screen.getByTestId("metrics-approvals-refused")).getByText("1"))
      .toBeInTheDocument();
    expect(within(screen.getByTestId("metrics-approvals-median")).getByText("13.3s"))
      .toBeInTheDocument();
  });

  it("keeps policy refusals out of the denial count and says why", async () => {
    // Merging them would claim an approver rejected sixteen actions nobody showed them.
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(approvals())));

    render(<MetricsApprovals token="tok" />);

    const note = await screen.findByTestId("metrics-approvals-policy-note");
    expect(note.textContent).toContain("16");
    expect(note.textContent).toMatch(/without anybody being asked/);
    expect(within(screen.getByTestId("metrics-approvals-refused")).getByText("1"))
      .toBeInTheDocument();
  });

  it("raises the expired tile and explains what an expiry costs", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(approvals())));

    render(<MetricsApprovals token="tok" />);

    const tile = await screen.findByTestId("metrics-approvals-expired");
    expect(tile.className).toContain("amber");
    expect(screen.getByTestId("metrics-approvals-expired-note").textContent)
      .toMatch(/never ran/);
  });

  it("names an ask that was neither answered nor expired", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(approvals())));

    render(<MetricsApprovals token="tok" />);

    expect((await screen.findByTestId("metrics-approvals-unanswered-note")).textContent)
      .toMatch(/fell through/);
  });

  it("says there is no refusal rate rather than showing 0%", async () => {
    // A zero share would read as "this approver refuses nothing", which is a claim about a person.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(approvals({
          answered: 0,
          refused: 0,
          refusal_share: null,
          median_seconds_to_answer: null,
          fastest_seconds: null,
          slowest_seconds: null,
        }))
      ),
    );

    render(<MetricsApprovals token="tok" />);

    expect((await screen.findByText(/no refusal rate to read/))).toBeInTheDocument();
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
  });

  it("says the view is missing rather than that nobody approved anything", async () => {
    // The 503 a gateway with no gate runtime answers.
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new Error("503");
    }));

    render(<MetricsApprovals token="tok" />);

    expect(await screen.findByTestId("metrics-approvals-unavailable")).toBeInTheDocument();
    expect(screen.queryByTestId("metrics-approvals-asked")).not.toBeInTheDocument();
  });

  it("carries its own window, because a decision is not a token", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(approvals({ window_days: 90 })));
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    render(<MetricsApprovals token="tok" />);
    await screen.findByTestId("metrics-approvals-asked");

    await user.click(screen.getByTestId("metrics-approvals-window-90"));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([input]) => String(input).includes("window=90")),
      ).toBe(true);
    });
  });
});
