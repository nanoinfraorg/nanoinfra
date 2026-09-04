/**
 * Device memory in the WebUI: the panel on a server's page (#229) and the gallery marker (#225).
 *
 * Two things a reader has to be able to tell at a glance, and both are assertions here rather than
 * a screenshot: which entry is the newest, and which entry a person wrote. An operator's note
 * outranks an agent's (#228), so a panel that rendered them identically would hide the one fact
 * that decides what happens next.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ServerNotesPanel } from "@/components/servers/ServerNotesPanel";
import type { NanoinfraClient } from "@/lib/nanoinfra-client";
import type { ConnectionStatus } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

const SERVER_ID = "a".repeat(32);

const NOTES = {
  serverId: SERVER_ID,
  name: "barrahome",
  notesUpdatedAt: "2026-09-03T10:00:00+00:00",
  text: "raw markdown here",
  entries: [
    {
      when: "2026-08-14 09:00 UTC",
      author: "alberto",
      title: "journald is deliberate",
      body: "The debug level is on purpose. Do not change it.",
      isOperator: true,
    },
    {
      when: "2026-09-03 10:00 UTC",
      author: "sre-copilot",
      title: "disk pressure",
      body: "Vacuumed /var/log/journal from 14G.",
      isOperator: false,
    },
  ],
  hasArchive: false,
};

function fakeClient(): NanoinfraClient {
  return {
    status: "open",
    operatorActor: "webui",
    onStatus: (handler: (value: ConnectionStatus) => void) => {
      handler("open");
      return () => {};
    },
    onOperatorActor: (handler: (value: string | null) => void) => {
      handler("webui");
      return () => {};
    },
  } as unknown as NanoinfraClient;
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function renderPanel() {
  return render(
    <ClientProvider client={fakeClient()} token="tok">
      <ServerNotesPanel serverId={SERVER_ID} />
    </ClientProvider>,
  );
}

describe("ServerNotesPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the newest entry first and marks the operator's", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(NOTES)));

    renderPanel();

    expect(await screen.findByText("disk pressure")).toBeInTheDocument();
    const titles = screen
      .getAllByText(/journald is deliberate|disk pressure/)
      .map((node) => node.textContent);
    expect(titles).toEqual(["disk pressure", "journald is deliberate"]);
    expect(screen.getByText("operator")).toBeInTheDocument();
    expect(screen.getByText(/sre-copilot/)).toBeInTheDocument();
  });

  it("appends a note without sending an author", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(NOTES));
    vi.stubGlobal("fetch", fetchMock);

    renderPanel();
    await screen.findByText("disk pressure");

    await userEvent.click(screen.getByRole("button", { name: /Add note/ }));
    await userEvent.type(screen.getByLabelText("Note title"), "needs sudo -n");
    await userEvent.type(screen.getByLabelText("Note body"), "Interactive prompts never answer.");
    await userEvent.click(screen.getByRole("button", { name: /Append/ }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });
    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(url).toContain(`/api/webui/servers/${SERVER_ID}/notes/append`);
    const headers = (init.headers ?? {}) as Record<string, string>;
    const body = decodeURIComponent(headers["X-Nanoinfra-Server-Notes-0"]);
    expect(JSON.parse(body)).toEqual({
      title: "needs sudo -n",
      body: "Interactive prompts never answer.",
    });
    // The author is the gateway's to decide, so it is not in what the panel sends.
    expect(body).not.toContain("author");
  });

  it("hands over the whole markdown file when a human edits it", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(NOTES));
    vi.stubGlobal("fetch", fetchMock);

    renderPanel();
    await screen.findByText("disk pressure");

    await userEvent.click(screen.getByRole("button", { name: /^Edit$/ }));
    const editor = screen.getByLabelText("Notes markdown");
    expect(editor).toHaveValue("raw markdown here");

    await userEvent.clear(editor);
    await userEvent.type(editor, "rewritten by a person");
    await userEvent.click(screen.getByRole("button", { name: /Save notes/ }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });
    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(url).toContain(`/api/webui/servers/${SERVER_ID}/notes/save`);
    const headers = (init.headers ?? {}) as Record<string, string>;
    expect(
      JSON.parse(decodeURIComponent(headers["X-Nanoinfra-Server-Notes-0"])),
    ).toEqual({ text: "rewritten by a person" });
  });

  it("states the reason the gateway gave for a refused note", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(NOTES))
      .mockResolvedValueOnce(
        new Response("Refusing to write this note: it contains a long hex run (32+ chars).", {
          status: 400,
          headers: { "content-type": "text/plain" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    renderPanel();
    await screen.findByText("disk pressure");

    await userEvent.click(screen.getByRole("button", { name: /Add note/ }));
    await userEvent.type(screen.getByLabelText("Note title"), "creds");
    await userEvent.type(screen.getByLabelText("Note body"), "0123456789abcdef0123456789abcdef");
    await userEvent.click(screen.getByRole("button", { name: /Append/ }));

    expect(await screen.findByText(/Refusing to write this note/)).toBeInTheDocument();
  });
});

describe("ServerList notes marker", () => {
  it("says a box has memory without reading a word of it", async () => {
    const { ServerList } = await import("@/components/servers/ServerList");
    render(
      <ServerList
        servers={[
          {
            id: SERVER_ID,
            name: "barrahome",
            providerId: "ssh",
            tags: [],
            updatedAt: "2026-09-01T00:00:00+00:00",
            notesUpdatedAt: "2026-09-03T10:00:00+00:00",
          },
          {
            id: "b".repeat(32),
            name: "no-memory",
            providerId: "ssh",
            tags: [],
            updatedAt: "2026-09-01T00:00:00+00:00",
            notesUpdatedAt: null,
          },
        ]}
        onOpen={() => {}}
        onNew={() => {}}
        onDelete={() => {}}
      />,
    );

    expect(screen.getAllByText(/^Notes /)).toHaveLength(1);
  });
});
