import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useDiagrams } from "@/hooks/useDiagrams";
import type { DiagramUpdateHandler } from "@/lib/nanoinfra-client";
import * as api from "@/lib/api";
import { ClientProvider } from "@/providers/ClientProvider";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    fetchDiagrams: vi.fn(),
  };
});

function fakeClient() {
  const diagramUpdateHandlers = new Set<DiagramUpdateHandler>();
  return {
    status: "open" as const,
    defaultChatId: null as string | null,
    onStatus: () => () => {},
    onError: () => () => {},
    onChat: () => () => {},
    onSessionUpdate: () => () => {},
    getRunStartedAt: () => null,
    onDiagramUpdate: (handler: DiagramUpdateHandler) => {
      diagramUpdateHandlers.add(handler);
      return () => diagramUpdateHandlers.delete(handler);
    },
    emitDiagramUpdate: (id: string, kind = "updated") => {
      for (const handler of diagramUpdateHandlers) handler(id, kind);
    },
    sendMessage: vi.fn(),
    newChat: vi.fn(),
    attach: vi.fn(),
    connect: vi.fn(),
    close: vi.fn(),
    updateUrl: vi.fn(),
  };
}

function wrap(client: ReturnType<typeof fakeClient>) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <ClientProvider
        client={client as unknown as import("@/lib/nanoinfra-client").NanoinfraClient}
        token="tok"
      >
        {children}
      </ClientProvider>
    );
  };
}

const listSpy = vi.mocked(api.fetchDiagrams);

describe("useDiagrams", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listSpy.mockResolvedValue({ diagrams: [] });
  });

  it("refetches the gallery when the server reports a diagram write", async () => {
    const client = fakeClient();
    const { result } = renderHook(() => useDiagrams(), { wrapper: wrap(client) });
    await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(1));
    expect(result.current.diagrams).toEqual([]);

    listSpy.mockResolvedValue({
      diagrams: [
        { id: "a".repeat(32), name: "From the agent", targets: [], nodeCount: 3, updatedAt: "now" },
      ],
    });
    client.emitDiagramUpdate("a".repeat(32), "created");

    await waitFor(() => expect(result.current.diagrams).toHaveLength(1));
    expect(result.current.diagrams[0].name).toBe("From the agent");
  });

  it("coalesces a burst of writes into one request", async () => {
    const client = fakeClient();
    renderHook(() => useDiagrams(), { wrapper: wrap(client) });
    await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(1));

    // The agent creating several diagrams in one turn, or renaming one twice.
    client.emitDiagramUpdate("a".repeat(32), "created");
    client.emitDiagramUpdate("b".repeat(32), "created");
    client.emitDiagramUpdate("b".repeat(32), "updated");

    await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(2));
    // Give the trailing window room to fire again if it were not coalescing.
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(listSpy).toHaveBeenCalledTimes(2);
  });

  it("stops listening once unmounted", async () => {
    const client = fakeClient();
    const { unmount } = renderHook(() => useDiagrams(), { wrapper: wrap(client) });
    await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(1));

    unmount();
    client.emitDiagramUpdate("a".repeat(32), "updated");
    await new Promise((resolve) => setTimeout(resolve, 400));

    expect(listSpy).toHaveBeenCalledTimes(1);
  });
});
