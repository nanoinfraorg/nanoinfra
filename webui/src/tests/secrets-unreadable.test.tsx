/**
 * A store this process may not read is not an empty store.
 *
 * The container hands the credential store to the executor account, so the process serving this
 * page is refused by the kernel. It answers 409 and says so. The page used to show "No secrets
 * yet" about a store holding an SSH key, which is the one thing it must not say.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SecretsView } from "@/components/secrets/SecretsView";
import type { NanoinfraClient } from "@/lib/nanoinfra-client";
import type { ConnectionStatus } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

const REASON =
  "the secret store at /home/nanoinfra/.nanoinfra/workspace/secrets exists and this process may not read it: [Errno 13] Permission denied";

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

function renderView() {
  return render(
    <ClientProvider client={fakeClient()} token="tok">
      <SecretsView />
    </ClientProvider>,
  );
}

describe("Secrets view when the store cannot be read", () => {
  it("states the reason the server gave, and shows no empty list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(REASON, {
          status: 409,
          headers: { "content-type": "text/plain" },
        }),
      ),
    );

    renderView();

    expect(await screen.findByText(new RegExp("may not read it"))).toBeInTheDocument();
    // The old page said this about a store holding a credential.
    expect(screen.queryByText("No secrets yet")).not.toBeInTheDocument();
  });

  it("still shows the empty state for a store that really is empty", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ secrets: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );

    renderView();

    expect(await screen.findByText("No secrets yet")).toBeInTheDocument();
  });
});
