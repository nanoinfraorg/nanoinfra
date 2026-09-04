/**
 * The automations editor's agent field -- nanoinfraorg/nanoinfra#257.
 *
 * The absence test is the one that matters most: no deployment names an agent yet, so the field
 * has to be invisible rather than an empty picker for a concept nobody configured.
 */
import { render, renderHook, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

import {
  AutomationAgentField,
  useNamedAgents,
} from "@/components/settings/AutomationAgentField";
import type { NamedAgentSummary } from "@/lib/api";

const fetchNamedAgents = vi.fn();

vi.mock("@/lib/api", () => ({
  fetchNamedAgents: (...args: unknown[]) => fetchNamedAgents(...args),
}));

const tx = (_key: string, fallback: string) => fallback;

const ROSTER: NamedAgentSummary[] = [
  { name: "sre-copilot", description: "Checks hosts and reads logs" },
  { name: "db-expert", description: "Postgres and slow queries" },
];

beforeEach(() => {
  fetchNamedAgents.mockReset();
});

describe("the agent field on an automation", () => {
  it("does not appear when the deployment names no agents", () => {
    const { container } = render(
      <AutomationAgentField agents={[]} value="" onChange={() => {}} tx={tx} />,
    );

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("lists every configured agent with its description", () => {
    render(<AutomationAgentField agents={ROSTER} value="" onChange={() => {}} tx={tx} />);

    expect(
      screen.getByRole("option", { name: "sre-copilot — Checks hosts and reads logs" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "db-expert — Postgres and slow queries" }),
    ).toBeInTheDocument();
  });

  it("defaults to the deployment default agent", () => {
    render(<AutomationAgentField agents={ROSTER} value="" onChange={() => {}} tx={tx} />);

    // The empty value is what the job record stores for "no agent named", so an unedited job
    // keeps behaving exactly as it did before this field existed.
    expect(screen.getByRole("combobox")).toHaveValue("");
    expect(screen.getByRole("option", { name: "Default agent" })).toBeInTheDocument();
  });

  it("shows the chosen agent's description as the help text", () => {
    render(
      <AutomationAgentField agents={ROSTER} value="db-expert" onChange={() => {}} tx={tx} />,
    );

    expect(screen.getByRole("combobox")).toHaveValue("db-expert");
    expect(screen.getByText("Postgres and slow queries")).toBeInTheDocument();
  });
});

describe("reading the roster", () => {
  it("reads the configured agents once there is a token", async () => {
    fetchNamedAgents.mockResolvedValue({ agents: ROSTER });

    const { result } = renderHook(() => useNamedAgents("test-token"));

    await waitFor(() => expect(result.current).toHaveLength(2));
    expect(fetchNamedAgents).toHaveBeenCalledWith("test-token", "");
  });

  it("treats a failed read as no named agents", async () => {
    // The route is an addition to an editor that worked without it. A gateway that cannot answer
    // must leave the rest of the form usable rather than block the save behind a field the
    // deployment may not even use.
    fetchNamedAgents.mockRejectedValue(new Error("route not registered"));

    const { result } = renderHook(() => useNamedAgents("test-token"));

    await waitFor(() => expect(fetchNamedAgents).toHaveBeenCalled());
    expect(result.current).toEqual([]);
  });
});
