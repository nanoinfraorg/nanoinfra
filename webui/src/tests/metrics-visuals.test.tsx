/**
 * The visual half of Live — nanoinfraorg/nanoinfra#274.
 *
 * A sparkline and a meter can lie in ways a number cannot, and each test below pins one of those
 * lies shut:
 *
 * - a single sample drawn as a line claims a trend from one reading
 * - a flat series stretched to fill its box turns "nothing happened" into a wiggle
 * - a gap in the samples drawn as a straight segment claims readings nobody took
 * - a ratio with no denominator rendered as 0% claims a measurement
 * - a cumulative counter plotted directly is a line that only climbs and says nothing about now
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MetricsCharts } from "@/components/metrics/MetricsCharts";
import { MetricsLive } from "@/components/metrics/MetricsLive";
import { Meter } from "@/components/metrics/Meter";
import { Sparkline } from "@/components/metrics/Sparkline";

function jsonResponse(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("Sparkline", () => {
  it("draws nothing from a single sample", () => {
    // One reading is a dot. A line through it would claim a direction nobody measured.
    const { container } = render(<Sparkline values={[5]} />);

    expect(container.querySelector("svg")).toBeNull();
  });

  it("draws nothing when every sample is unreadable", () => {
    const { container } = render(<Sparkline values={[null, null, null]} />);

    expect(container.querySelector("svg")).toBeNull();
  });

  it("keeps a flat series flat instead of stretching it to fill the box", () => {
    /*
     * The most common way a sparkline lies. Three minutes of `0` should look like nothing
     * happening; normalising a zero-span series to the full height turns it into a wiggle.
     */
    const { container } = render(<Sparkline values={[0, 0, 0, 0]} />);

    const path = container.querySelector("path[stroke='currentColor']");
    expect(path).not.toBeNull();
    const ys = [...(path?.getAttribute("d") ?? "").matchAll(/,(-?[\d.]+)/g)].map((m) => m[1]);
    expect(new Set(ys).size).toBe(1);
  });

  it("breaks the line across a sample it could not read", () => {
    // Two runs, not one path bridging the gap: a straight segment there would be a reading.
    const { container } = render(<Sparkline values={[1, 2, null, 8, 9]} />);

    const strokes = container.querySelectorAll("path[stroke='currentColor']");
    expect(strokes.length).toBe(2);
  });
});

describe("Meter", () => {
  const format = (value: number) => String(value);

  it("does the division the two tiles used to leave to the reader", () => {
    render(<Meter label="Context window" used={250} limit={1_000} format={format} testId="m" />);

    const meter = screen.getByTestId("m");
    expect(within(meter).getByText("25%")).toBeInTheDocument();
    expect(within(meter).getByText("250 of 1000")).toBeInTheDocument();
  });

  it("moves the fill to warning and then to critical, and always labels the percentage", () => {
    // A status colour never carries meaning alone, so the number is present at every severity.
    const { rerender } = render(
      <Meter label="c" used={500} limit={1_000} format={format} testId="m" />,
    );
    expect(screen.getByTestId("m")).toHaveAttribute("data-severity", "normal");

    rerender(<Meter label="c" used={800} limit={1_000} format={format} testId="m" />);
    expect(screen.getByTestId("m")).toHaveAttribute("data-severity", "warning");
    expect(screen.getByText("80%")).toBeInTheDocument();

    rerender(<Meter label="c" used={950} limit={1_000} format={format} testId="m" />);
    expect(screen.getByTestId("m")).toHaveAttribute("data-severity", "critical");
    expect(screen.getByText("95%")).toBeInTheDocument();
  });

  it("reads a missing limit as unknown rather than as 0%", () => {
    render(<Meter label="c" used={250} limit={null} format={format} testId="m" />);

    expect(screen.getByTestId("m")).toHaveAttribute("data-severity", "unknown");
    expect(screen.getByText("No limit reported")).toBeInTheDocument();
    expect(screen.queryByText("0%")).not.toBeInTheDocument();
  });

  it("does not divide by a limit of zero", () => {
    render(<Meter label="c" used={250} limit={0} format={format} testId="m" />);

    expect(screen.getByTestId("m")).toHaveAttribute("data-severity", "unknown");
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("exposes itself as a meter to a screen reader", () => {
    render(<Meter label="Context window" used={250} limit={1_000} format={format} testId="m" />);

    const meter = screen.getByRole("meter", { name: "Context window" });
    expect(meter).toHaveAttribute("aria-valuenow", "25");
  });
});

describe("Live, with the visual half", () => {
  const gauge = (name: string, value: number | null, alerting = false) => ({
    name,
    value,
    label: name,
    help: `help for ${name}`,
    alerting,
  });

  it("folds the context pair into one meter instead of two tiles", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/metrics/live")
          ? jsonResponse({
            available: true,
            gauges: [
              gauge("nanoinfra_ws_connections", 1),
              gauge("nanoinfra_context_tokens_used", 428_000),
              gauge("nanoinfra_context_tokens_limit", 1_048_576),
            ],
          })
          : new Promise<Response>(() => {})
      ),
    );

    render(<MetricsLive token="tok" />);

    return waitFor(() => {
      expect(screen.getByTestId("metrics-context-meter")).toBeInTheDocument();
      // Neither half survives as its own tile.
      expect(screen.queryByTestId("metrics-gauge-nanoinfra_context_tokens_used")).toBeNull();
      expect(screen.queryByTestId("metrics-gauge-nanoinfra_context_tokens_limit")).toBeNull();
    });
  });

  it("gives an unreadable gauge its dash and no sparkline", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/metrics/live")
          ? jsonResponse({ available: true, gauges: [gauge("nanoinfra_ws_connections", null)] })
          : new Promise<Response>(() => {})
      ),
    );

    render(<MetricsLive token="tok" />);

    const tile = await screen.findByTestId("metrics-gauge-nanoinfra_ws_connections");
    expect(within(tile).getByText("—")).toBeInTheDocument();
    // An empty plot would read as flat at zero, which is the lie this panel avoids.
    expect(tile.querySelector("svg")).toBeNull();
  });
});

describe("MetricsCharts", () => {
  const counters = (llm: number, tool: number) => ({
    counters: [
      { name: "nanoinfra_llm_calls_total", labels: { provider: "m", model: "k" }, value: llm },
      { name: "nanoinfra_tool_calls_total", labels: { tool: "exec" }, value: tool },
    ],
    histograms: [],
  });

  it("renders nothing until it can draw a line, which takes three reads", async () => {
    /*
     * One read of a cumulative counter is a total, not a rate. Two reads give one rate point, and
     * a point is not a line — so the chart waits for three, which is the same minimum the
     * sparkline enforces for the same reason.
     */
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(counters(10, 4))));

    const { container } = render(<MetricsCharts token="tok" />);
    await waitFor(() => expect(container.querySelector("svg")).toBeNull());

    await vi.advanceTimersByTimeAsync(3_100);
    expect(screen.queryByTestId("metrics-rate-chart")).toBeNull();
  });

  it("plots the difference between reads, and direct-labels both series", async () => {
    /*
     * Direct labels are mandatory rather than decorative: `validate_palette.js` returned a
     * contrast WARN for categorical slot 2 on this WebUI's light surface (2.99:1), and a WARN
     * obligates a visible label instead of colour alone.
     */
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let reads = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      reads += 1;
      return jsonResponse(counters(reads * 5, reads * 2));
    }));

    render(<MetricsCharts token="tok" />);
    // Three reads: a rate is the difference between a pair, so two samples give one point and a
    // line needs two — the same minimum the sparkline enforces.
    await vi.advanceTimersByTimeAsync(3_100);
    await vi.advanceTimersByTimeAsync(3_100);

    await waitFor(() => {
      expect(screen.getByTestId("metrics-rate-chart")).toBeInTheDocument();
    });
    expect(screen.getByTestId("metrics-rate-legend-0")).toHaveTextContent("LLM calls");
    expect(screen.getByTestId("metrics-rate-legend-1")).toHaveTextContent("Tool calls");
  });

  it("keeps an empty latency bucket's row, because a band with no calls is information", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          counters: [],
          histograms: [{
            name: "nanoinfra_llm_duration_ms",
            labels: { provider: "m", model: "k" },
            buckets: [
              { le: 500, count: 138 },
              { le: 1000, count: 61 },
              { le: 2500, count: 0 },
              { le: null, count: 0 },
            ],
            sum: 100_000,
            count: 199,
          }],
        })
      ),
    );

    render(<MetricsCharts token="tok" />);

    const histogram = await screen.findByTestId("metrics-latency-histogram");
    expect(within(histogram).getByText("138")).toBeInTheDocument();
    // Every bucket prints its count, including the two zeros.
    expect(within(histogram).getAllByText("0")).toHaveLength(2);
    expect(screen.getByTestId("metrics-latency-bucket-3")).toBeInTheDocument();
  });

  it("renders nothing at all when no counter has ever fired", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ counters: [], histograms: [] })));

    const { container } = render(<MetricsCharts token="tok" />);

    await waitFor(() => expect(container.firstChild).toBeNull());
  });
});

describe("the charts cannot take the Live tab down", () => {
  it("renders nothing when the route answers a shape it did not expect", async () => {
    /*
     * The same failure `MetricsScaleRow` had and this did not learn from on the first pass: these
     * charts render inside the Live tab, so trusting `payload.counters` to be an array meant an
     * older gateway — or any unexpected answer — unmounted every gauge above them.
     */
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ nope: true })));

    const { container } = render(<MetricsCharts token="tok" />);

    await waitFor(() => expect(container.firstChild).toBeNull());
  });

  it("leaves the gauges standing when the counters route is unavailable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).includes("/metrics/live")
          ? jsonResponse({
            available: true,
            gauges: [{
              name: "nanoinfra_ws_connections",
              value: 2,
              label: "WebUI sockets",
              help: "h",
              alerting: false,
            }],
          })
          : jsonResponse({ unexpected: "shape" })
      ),
    );

    render(<MetricsLive token="tok" />);

    // The gauge is what matters; the charts are the optional half.
    const tile = await screen.findByTestId("metrics-gauge-nanoinfra_ws_connections");
    expect(within(tile).getByText("2")).toBeInTheDocument();
    expect(screen.queryByTestId("metrics-charts")).toBeNull();
  });
});
