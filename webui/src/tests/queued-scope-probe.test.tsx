import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ThreadComposer } from "@/components/thread/ThreadComposer";

describe("probe: queued prompts across a switch to an idle session", () => {
  it("must not send chat-a's queued prompt when switching to idle chat-b", () => {
    const sendA = vi.fn();
    const sendB = vi.fn();
    const view = render(
      <ThreadComposer onSend={sendA} onStop={vi.fn()} isStreaming pendingQueueKey="chat-a"
        placeholder="Type your message..." />,
    );
    const input = screen.getByLabelText("Message input");
    fireEvent.change(input, { target: { value: "follow-up for A" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByText("follow-up for A")).toBeInTheDocument();
    expect(sendA).not.toHaveBeenCalled();

    // chat-b is idle. Arriving at an idle session is not completion of chat-a's run.
    view.rerender(
      <ThreadComposer onSend={sendB} onStop={vi.fn()} isStreaming={false} pendingQueueKey="chat-b"
        placeholder="Type your message..." />,
    );

    expect(sendB).not.toHaveBeenCalled();
    expect(sendA).not.toHaveBeenCalled();
  });
});
