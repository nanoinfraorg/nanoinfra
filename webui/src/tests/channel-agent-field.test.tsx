/**
 * The channel's agent binding in Settings -> Channels -- `channels.<name>.agent`.
 *
 * Two tests carry the design. The absence test, because no deployment names an agent until an
 * operator writes a roster, so the section has to be invisible rather than an empty picker. And
 * the WebSocket test, because that channel is the one refusal: its agent is chosen per message in
 * the composer, and a gap where the section would be reads as a channel that forgot the feature.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ChannelAgentField } from "@/components/settings/channels/ChannelAgentField";
import type { NamedAgentSummary } from "@/lib/api";

const tx = (_key: string, fallback: string) => fallback;

const ROSTER: NamedAgentSummary[] = [
  { name: "sre", description: "Checks hosts and reads logs" },
  { name: "triage", description: "Reads the inbox, sends nothing" },
];

describe("the agent binding section", () => {
  it("does not appear when the deployment names no agents", () => {
    const { container } = render(
      <ChannelAgentField channel="telegram" agents={[]} value="" onChange={() => {}} tx={tx} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("offers every configured agent, and the deployment default", () => {
    render(
      <ChannelAgentField channel="telegram" agents={ROSTER} value="" onChange={() => {}} tx={tx} />,
    );

    expect(screen.getByText("Answering agent")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Default agent" })).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "sre — Checks hosts and reads logs" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "triage — Reads the inbox, sends nothing" }),
    ).toBeInTheDocument();
  });

  it("shows the bound agent as the current value", () => {
    render(
      <ChannelAgentField channel="discord" agents={ROSTER} value="sre" onChange={() => {}} tx={tx} />,
    );

    expect(screen.getByRole("combobox")).toHaveValue("sre");
  });

  it("defaults to the empty value, which is how no binding is stored", () => {
    render(
      <ChannelAgentField channel="discord" agents={ROSTER} value="" onChange={() => {}} tx={tx} />,
    );

    expect(screen.getByRole("combobox")).toHaveValue("");
  });

  it("says a sender can still name another agent", () => {
    render(
      <ChannelAgentField channel="slack" agents={ROSTER} value="" onChange={() => {}} tx={tx} />,
    );

    // The mention wins over the binding, and the help says so: an operator who binds an agent
    // should not be surprised that `@agent:` still routes.
    expect(screen.getByText(/@agent:<name>/)).toBeInTheDocument();
  });

  it("reports the chosen agent's name back", async () => {
    const onChange = vi.fn();
    const { default: userEvent } = await import("@testing-library/user-event");
    render(
      <ChannelAgentField channel="telegram" agents={ROSTER} value="" onChange={onChange} tx={tx} />,
    );

    await userEvent.selectOptions(screen.getByRole("combobox"), "triage");

    expect(onChange).toHaveBeenCalledWith("triage");
  });

  it("reports the empty value when the operator returns to the default", async () => {
    const onChange = vi.fn();
    const { default: userEvent } = await import("@testing-library/user-event");
    render(
      <ChannelAgentField channel="telegram" agents={ROSTER} value="sre" onChange={onChange} tx={tx} />,
    );

    await userEvent.selectOptions(screen.getByRole("combobox"), "");

    // The empty value has to reach the server, because there it means *clear the field* rather
    // than *leave it alone*. A picker that reported nothing would leave `sre` bound.
    expect(onChange).toHaveBeenCalledWith("");
  });
});

describe("the WebSocket channel", () => {
  it("states why it has no picker rather than showing none", () => {
    render(
      <ChannelAgentField channel="websocket" agents={ROSTER} value="" onChange={() => {}} tx={tx} />,
    );

    expect(screen.getByText("Answering agent")).toBeInTheDocument();
    expect(screen.getByText(/Chosen for each message in the composer/)).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("says nothing at all when the deployment names no agents" , () => {
    const { container } = render(
      <ChannelAgentField channel="websocket" agents={[]} value="" onChange={() => {}} tx={tx} />,
    );

    // No roster means the concept is not in play, so the explanation would raise a question
    // rather than answer one.
    expect(container).toBeEmptyDOMElement();
  });
});
