import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AgentActivityCluster } from "@/components/thread/AgentActivityCluster";
import type { UIMessage } from "@/lib/types";

function activityMessages(extraReasoning = "", extraTool?: UIMessage): UIMessage[] {
  const rows: UIMessage[] = [
    {
      id: "r1",
      role: "assistant",
      content: "",
      reasoning: `thinking${extraReasoning}`,
      reasoningStreaming: true,
      isStreaming: true,
      createdAt: 1,
    },
    {
      id: "t1",
      role: "tool",
      kind: "trace",
      content: "search()",
      traces: ["search()"],
      createdAt: 2,
    },
  ];
  if (extraTool) rows.push(extraTool);
  return rows;
}

function installAnimationFrameQueue() {
  const originalRequest = window.requestAnimationFrame;
  const originalCancel = window.cancelAnimationFrame;
  const callbacks = new Map<number, FrameRequestCallback>();
  let nextId = 1;

  window.requestAnimationFrame = ((callback: FrameRequestCallback) => {
    const id = nextId;
    nextId += 1;
    callbacks.set(id, callback);
    return id;
  }) as typeof window.requestAnimationFrame;
  window.cancelAnimationFrame = ((id: number) => {
    callbacks.delete(id);
  }) as typeof window.cancelAnimationFrame;

  return {
    flush() {
      const pending = Array.from(callbacks.entries());
      callbacks.clear();
      for (const [, callback] of pending) callback(0);
    },
    restore() {
      window.requestAnimationFrame = originalRequest;
      window.cancelAnimationFrame = originalCancel;
    },
  };
}

function setScrollGeometry(
  element: HTMLElement,
  geometry: { scrollHeight: number; clientHeight: number; scrollTop?: number },
) {
  Object.defineProperties(element, {
    scrollHeight: { configurable: true, value: geometry.scrollHeight },
    clientHeight: { configurable: true, value: geometry.clientHeight },
    scrollTop: {
      configurable: true,
      value: geometry.scrollTop ?? element.scrollTop,
      writable: true,
    },
  });
}

describe("AgentActivityCluster", () => {
  it("jumps to the latest activity when opened", () => {
    const raf = installAnimationFrameQueue();
    try {
      render(
        <AgentActivityCluster
          messages={activityMessages()}
          isTurnStreaming
          hasBodyBelow={false}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: /working/i }));
      const scrollport = screen.getByTestId("agent-activity-scroll");
      setScrollGeometry(scrollport, {
        scrollHeight: 1000,
        clientHeight: 120,
        scrollTop: 0,
      });

      act(() => {
        raf.flush();
      });

      expect(scrollport.scrollTop).toBe(880);
    } finally {
      raf.restore();
    }
  });

  it("follows new reasoning and tool activity while the user is at the bottom", () => {
    const raf = installAnimationFrameQueue();
    try {
      const { rerender } = render(
        <AgentActivityCluster
          messages={activityMessages()}
          isTurnStreaming
          hasBodyBelow={false}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: /working/i }));
      const scrollport = screen.getByTestId("agent-activity-scroll");
      setScrollGeometry(scrollport, {
        scrollHeight: 1000,
        clientHeight: 120,
        scrollTop: 0,
      });
      act(() => {
        raf.flush();
      });

      rerender(
        <AgentActivityCluster
          messages={activityMessages(" with more detail", {
            id: "t2",
            role: "tool",
            kind: "trace",
            content: "open_browser()",
            traces: ["open_browser()"],
            createdAt: 3,
          })}
          isTurnStreaming
          hasBodyBelow={false}
        />,
      );
      setScrollGeometry(scrollport, {
        scrollHeight: 1500,
        clientHeight: 120,
        scrollTop: scrollport.scrollTop,
      });

      act(() => {
        raf.flush();
      });

      expect(scrollport.scrollTop).toBe(1380);
    } finally {
      raf.restore();
    }
  });

  it("does not pull the user down after they scroll up inside the activity pane", () => {
    const raf = installAnimationFrameQueue();
    try {
      const { rerender } = render(
        <AgentActivityCluster
          messages={activityMessages()}
          isTurnStreaming
          hasBodyBelow={false}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: /working/i }));
      const scrollport = screen.getByTestId("agent-activity-scroll");
      setScrollGeometry(scrollport, {
        scrollHeight: 1000,
        clientHeight: 120,
        scrollTop: 0,
      });
      act(() => {
        raf.flush();
      });

      scrollport.scrollTop = 100;
      fireEvent.scroll(scrollport);

      rerender(
        <AgentActivityCluster
          messages={activityMessages(" still streaming")}
          isTurnStreaming
          hasBodyBelow={false}
        />,
      );
      setScrollGeometry(scrollport, {
        scrollHeight: 1500,
        clientHeight: 120,
        scrollTop: scrollport.scrollTop,
      });

      act(() => {
        raf.flush();
      });

      expect(scrollport.scrollTop).toBe(100);
    } finally {
      raf.restore();
    }
  });
});
