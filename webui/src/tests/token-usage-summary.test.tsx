import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TokenUsageSummary } from "@/components/settings/TokenUsageSummary";
import type { SettingsPayload } from "@/lib/types";

type Usage = NonNullable<SettingsPayload["usage"]>;

function usage(overrides: Partial<Usage> = {}): Usage {
  return {
    // Two days, so "today" and "last 30 days" are different numbers -- the way
    // they are on any real deployment.
    days: [
      {
        date: "2026-08-29",
        prompt_tokens: 540_000,
        completion_tokens: 60_000,
        cached_tokens: 0,
        total_tokens: 600_000,
        provider_tokens: 540_000,
        estimated_tokens: 60_000,
        requests: 25,
      },
      {
        date: "2026-08-30",
        prompt_tokens: 360_000,
        completion_tokens: 40_000,
        cached_tokens: 0,
        total_tokens: 400_000,
        provider_tokens: 360_000,
        estimated_tokens: 40_000,
        requests: 15,
      },
    ],
    total_tokens: 1_000_000,
    total_tokens_30d: 1_000_000,
    total_tokens_365d: 1_000_000,
    peak_day_tokens: 1_000_000,
    current_streak_days: 3,
    longest_streak_days: 10,
    active_days_30d: 1,
    requests_30d: 40,
    failed_requests_30d: 4,
    providers_30d: [
      {
        provider: "anthropic",
        model: "claude-sonnet-5",
        total_tokens: 900_000,
        prompt_tokens: 820_000,
        completion_tokens: 80_000,
        cached_tokens: 0,
        provider_tokens: 900_000,
        estimated_tokens: 0,
        requests: 30,
        failed_requests: 2,
        ttft_ms: 36_000,
        timed_requests: 30,
        generation_ms: 60_000,
        measured_completion_tokens: 80_000,
      },
    ],
    updated_at: "2026-08-30",
    ...overrides,
  } as Usage;
}

describe("token usage summary", () => {
  it("writes the numbers out, because a grid of dots carries none", () => {
    render(<TokenUsageSummary usage={usage()} />);

    expect(screen.getByText(/1,000,000 tokens · 40 calls/)).toBeTruthy();
    expect(screen.getByText(/4 failed \(10%\)/)).toBeTruthy();
    // Today is its own line, not the same figure repeated.
    expect(screen.getByText(/400,000 tokens · 15 calls/)).toBeTruthy();
  });

  it("says how much of the total the provider actually measured", () => {
    // The partition is the thing the type made real, and a cost figure whose
    // origin is unstated invites arithmetic it cannot carry.
    render(<TokenUsageSummary usage={usage()} />);

    expect(screen.getByText(/90% reported by the provider/)).toBeTruthy();
    expect(screen.getByText(/100,000 estimated locally/)).toBeTruthy();
  });

  it("names the expensive model, which no day row could carry", () => {
    render(<TokenUsageSummary usage={usage()} />);

    expect(screen.getByText("claude-sonnet-5")).toBeTruthy();
    expect(screen.getByText("anthropic")).toBeTruthy();
    expect(screen.getByText(/1.2s to first token/)).toBeTruthy();
  });

  it("averages time to first token over the calls that were timed", () => {
    // Not over all of them: a call nobody timed would drag the figure to zero.
    render(
      <TokenUsageSummary
        usage={usage({
          providers_30d: [
            {
              ...usage().providers_30d![0],
              requests: 100,
              ttft_ms: 36_000,
              timed_requests: 30,
            },
          ],
        })}
      />,
    );

    expect(screen.getByText(/1.2s to first token/)).toBeTruthy();
  });

  it("says none failed rather than showing a zero", () => {
    render(<TokenUsageSummary usage={usage({ failed_requests_30d: 0 })} />);

    expect(screen.getByText(/none failed/)).toBeTruthy();
  });

  it("renders nothing at all without a payload", () => {
    const { container } = render(<TokenUsageSummary usage={undefined} />);

    expect(container.firstChild).toBeNull();
  });

  it("survives a payload from a gateway that does not send the new fields", () => {
    // An older gateway answers without `providers_30d` or `failed_requests_30d`;
    // the page has to render rather than throw.
    const older = usage();
    delete (older as Partial<Usage>).providers_30d;
    delete (older as Partial<Usage>).failed_requests_30d;

    render(<TokenUsageSummary usage={older} />);

    expect(screen.getByText(/1,000,000 tokens · 40 calls/)).toBeTruthy();
    expect(screen.queryByText("claude-sonnet-5")).toBeNull();
  });
});
